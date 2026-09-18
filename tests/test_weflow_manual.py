from __future__ import annotations

import json
import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from pkas import weflow_manual_worker
from pkas.system import KnowledgeSystem
from pkas.weflow_manual import export_record_token
from pkas.weflow_manual_worker import (
    ManualSyncOwnerExited,
    _committed_export_progress,
    _failure_code,
    _known_export_watermark,
    _new_export_candidates,
    _wait_for_new_export_records,
)

CHATLAB_FIXTURE = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"


def _seed_conversation(system: KnowledgeSystem, target: Path) -> None:
    target.write_bytes(CHATLAB_FIXTURE.read_bytes())
    inspection = system.weflow.inspect_chatlab_file(str(target))
    result = system.weflow.import_chatlab_file(
        path=str(target),
        inspection_token=inspection["inspection_token"],
        session_id=inspection["session_id"],
        privacy="restricted",
    )
    assert result["imported"] > 0


def test_manual_sync_status_uses_customer_watermark(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    knowledge_system.database.initialize()
    _seed_conversation(knowledge_system, source_root / "manual-chat.json")

    status = knowledge_system.weflow_manual.status()

    assert status["mode"] == "manual_only"
    assert status["scheduled"] is False
    assert status["autostart"] is False
    assert status["conversation_count"] == 1
    assert status["latest_message_timestamp"] is not None
    assert status["last_import_at"] is not None
    assert status["exported_sessions"] == 0
    assert status["no_data_sessions"] == 0
    assert status["export_failed_sessions"] == 0


def test_manual_status_reports_export_freshness_without_exposing_records(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    knowledge_system.database.initialize()
    _seed_conversation(knowledge_system, source_root / "manual-chat.json")
    session_id = knowledge_system.weflow_manual.sync_scope()["session_ids"][0]
    export = source_root / "latest-export.xlsx"
    export.write_bytes(b"placeholder")
    records = source_root / "weflow-export-records.json"
    records.write_text(
        json.dumps(
            {
                session_id: [
                    {
                        "exportTime": 1_700_000_000,
                        "messageCount": 2,
                        "outputPath": str(export),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    knowledge_system.weflow_manual.config_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    knowledge_system.weflow_manual.config_path.write_text(
        json.dumps({"records_path": str(records)}),
        encoding="utf-8",
    )

    initial = knowledge_system.weflow_manual.status()
    assert initial["export_freshness"] == "not_compared"
    assert initial["usable_export_count"] == 1
    assert initial["latest_export_at"] is not None
    knowledge_system.weflow_manual.state_path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_system.weflow_manual.state_path.write_text(
        json.dumps({"status": "completed", "export_watermark": 1_700_000_000}),
        encoding="utf-8",
    )
    assert knowledge_system.weflow_manual.status()["export_freshness"] == "current"
    records.write_text(
        json.dumps(
            {
                session_id: [
                    {
                        "exportTime": 1_700_000_001,
                        "messageCount": 2,
                        "outputPath": str(export),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert (
        knowledge_system.weflow_manual.status()["export_freshness"]
        == "new_export_available"
    )


def test_manual_status_detects_unseen_export_at_same_timestamp(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    knowledge_system.database.initialize()
    _seed_conversation(knowledge_system, source_root / "manual-chat.json")
    session_id = knowledge_system.weflow_manual.sync_scope()["session_ids"][0]
    export = source_root / "same-time-export.xlsx"
    export.write_bytes(b"placeholder")
    records = source_root / "weflow-export-records.json"
    original = {
        "session_id": session_id,
        "export_time": 1_700_000_000,
        "byte_size": export.stat().st_size,
        "message_count": 2,
    }
    records.write_text(
        json.dumps(
            {
                session_id: [
                    {
                        "exportTime": original["export_time"],
                        "messageCount": original["message_count"],
                        "outputPath": str(export),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    knowledge_system.weflow_manual.config_path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_system.weflow_manual.config_path.write_text(
        json.dumps({"records_path": str(records)}), encoding="utf-8"
    )
    knowledge_system.weflow_manual.state_path.parent.mkdir(parents=True, exist_ok=True)
    knowledge_system.weflow_manual.state_path.write_text(
        json.dumps(
            {
                "status": "completed",
                "export_watermark": original["export_time"],
                "export_record_tokens": [export_record_token(original)],
            }
        ),
        encoding="utf-8",
    )
    assert knowledge_system.weflow_manual.status()["export_freshness"] == "current"

    records.write_text(
        json.dumps(
            {
                session_id: [
                    {
                        "exportTime": original["export_time"],
                        "messageCount": 3,
                        "outputPath": str(export),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert knowledge_system.weflow_manual.status()["export_freshness"] == "new_export_available"


def test_new_export_candidates_handles_unseen_same_timestamp_without_legacy_replay() -> None:
    original = {
        "session_id": "session-a",
        "export_time": 10,
        "byte_size": 20,
        "message_count": 2,
    }
    replacement = {**original, "message_count": 3}
    catalog = {"items": [original, replacement]}

    assert _new_export_candidates(
        catalog,
        session_ids={"session-a"},
        baseline_export_time=10,
        known_export_tokens=set(),
    ) == []
    assert _new_export_candidates(
        catalog,
        session_ids={"session-a"},
        baseline_export_time=10,
        known_export_tokens={export_record_token(original)},
    ) == [replacement]


def test_manual_sync_start_spawns_hidden_worker_without_scheduling(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    knowledge_system.database.initialize()
    _seed_conversation(knowledge_system, source_root / "manual-chat.json")
    root = source_root / "weflow"
    for path in (
        root / "package.json",
        root / "node_modules" / "electron" / "dist" / "electron.exe",
        root / "node_modules" / "electron-store" / "index.js",
        knowledge_system.settings.project_root / "scripts" / "configure_weflow_manual.mjs",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    records = source_root / "weflow-export-records.json"
    records.write_text("{}", encoding="utf-8")

    captured: dict[str, object] = {}

    class Process:
        pid = 12345

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr("pkas.weflow_manual.subprocess.Popen", fake_popen)
    result = knowledge_system.weflow_manual.start(
        weflow_root=str(root),
        records_path=str(records),
    )

    assert result["status"] == "queued"
    assert result["scheduled"] is False
    assert result["autostart"] is False
    assert "pkas.weflow_manual_worker" in captured["command"]
    assert "--owner-pid" in captured["command"]
    assert captured["kwargs"]["stdout"] is not None
    config = json.loads(
        knowledge_system.weflow_manual.config_path.read_text(encoding="utf-8")
    )
    assert config["mode"] == "manual_only"


def test_manual_failure_codes_are_safe_and_actionable() -> None:
    assert _failure_code(RuntimeError("WeFlow 当前由用户打开；请退出。")) == (
        "weflow_already_open"
    )
    assert _failure_code(TimeoutError("WeFlow 导出超时。")) == "export_timeout"
    assert _failure_code(RuntimeError("WeFlow 导出失败 [database]。")) == (
        "weflow_export_database"
    )
    assert _failure_code(RuntimeError("任务状态失败 [helper_task_missing]。")) == (
        "weflow_export_helper_task_missing"
    )
    assert _failure_code(RuntimeError("导出没有产生记录 [no_output]。")) == (
        "weflow_export_no_output"
    )
    assert _failure_code(RuntimeError("包含私人路径的未知异常")) == (
        "manual_sync_failed"
    )


def test_manual_worker_creates_and_initializes_system_only_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    created: list[object] = []

    class Database:
        def initialize(self) -> None:
            raise AssertionError("worker must not initialize the same database twice")

    system = SimpleNamespace(
        database=Database(),
        settings=SimpleNamespace(data_root=tmp_path),
        weflow_manual=SimpleNamespace(
            sync_scope=lambda: {"session_ids": ["wxid_test"]}
        ),
    )
    monkeypatch.setattr(
        weflow_manual_worker.KnowledgeSystem,
        "create",
        lambda: created.append(system) or system,
    )
    monkeypatch.setattr(weflow_manual_worker, "WindowsFileLock", lambda _path: nullcontext())
    monkeypatch.setattr(weflow_manual_worker, "_weflow_is_running", lambda _root: True)

    with pytest.raises(RuntimeError, match="当前由用户打开"):
        weflow_manual_worker.run("test-job", tmp_path / "weflow", tmp_path / "records.json")

    assert created == [system]


def test_manual_worker_refuses_to_start_after_owner_exits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(weflow_manual_worker, "_owner_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        weflow_manual_worker.KnowledgeSystem,
        "create",
        lambda: (_ for _ in ()).throw(AssertionError("system must not start")),
    )

    with pytest.raises(ManualSyncOwnerExited, match="知枢已退出"):
        weflow_manual_worker.run(
            "test-job", tmp_path / "weflow", tmp_path / "records.json", owner_pid=12345
        )


def test_manual_worker_reuses_committed_watermark_for_import_retry(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime" / "weflow-manual-sync-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"status": "importing", "export_watermark": 1_700_000_000}),
        encoding="utf-8",
    )

    assert _known_export_watermark(tmp_path) == 1_700_000_000


def test_manual_worker_does_not_advance_watermark_on_partial_failure() -> None:
    previous_tokens = {"a" * 64}
    watermark, tokens = _committed_export_progress(
        {
            "status": "warning",
            "failed_sessions": 1,
            "export_watermark": 1_800_000_000,
            "export_record_tokens": ["b" * 64],
        },
        previous_watermark=1_700_000_000,
        previous_tokens=previous_tokens,
    )

    assert watermark == 1_700_000_000
    assert tokens == ["a" * 64]


def test_manual_worker_waits_for_same_timestamp_unseen_export() -> None:
    original = {
        "session_id": "session-a",
        "export_time": 10,
        "byte_size": 20,
        "message_count": 2,
    }
    replacement = {**original, "message_count": 3}
    system = SimpleNamespace(
        weflow=SimpleNamespace(
            discover_exports=lambda **_kwargs: {"items": [replacement]}
        )
    )

    assert _wait_for_new_export_records(
        system,
        records_path="records.json",
        session_ids={"session-a"},
        baseline_export_time=10,
        known_export_tokens={export_record_token(original)},
        timeout_seconds=1,
    ) is True


def test_manual_worker_main_does_not_precreate_knowledge_system(
    tmp_path: Path,
    monkeypatch,
) -> None:
    updates: list[dict[str, object]] = []
    monkeypatch.setattr(
        weflow_manual_worker,
        "get_settings",
        lambda: SimpleNamespace(data_root=tmp_path),
    )
    monkeypatch.setattr(
        weflow_manual_worker,
        "run",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "summary_code": "no_new_messages",
        },
    )
    monkeypatch.setattr(
        weflow_manual_worker,
        "update_manual_sync_state",
        lambda _root, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "weflow_manual_worker",
            "--job-id",
            "test-job",
            "--weflow-root",
            str(tmp_path / "weflow"),
            "--records-path",
            str(tmp_path / "records.json"),
        ],
    )

    weflow_manual_worker.main()

    assert len(updates) == 1
    update = updates[0]
    assert {key: value for key, value in update.items() if key != "completed_at"} == {
        "job_id": "test-job",
        "status": "completed",
        "stage": "completed",
        "summary_code": "no_new_messages",
        "imported_messages": 0,
        "duplicate_messages": 0,
        "failed_sessions": 0,
        "exported_sessions": 0,
        "no_data_sessions": 0,
        "export_failed_sessions": 0,
        "export_watermark": 0,
        "export_record_tokens": [],
        "phase_timings": {},
        "total_elapsed_seconds": 0.0,
        "export_progress_committed": False,
    }
    assert isinstance(update["completed_at"], str)


def test_weflow_helper_configures_and_cleans_isolated_store(tmp_path: Path) -> None:
    node = Path(r"C:\Program Files\nodejs\node.exe")
    weflow_root = Path(r"D:\wx\xwechat_files\tools\WeFlow")
    store_module = weflow_root / "node_modules" / "electron-store" / "index.js"
    helper = Path(__file__).parents[1] / "scripts" / "configure_weflow_manual.mjs"
    if not node.is_file() or not store_module.is_file():
        pytest.skip("Local WeFlow development runtime is unavailable")

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    scope_key = "test-db::test-user"
    (config_dir / "WeFlow-config.json").write_text(
        json.dumps(
            {
                "dbPath": "test-db",
                "myWxid": "test-user",
                "exportPath": str(tmp_path / "exports"),
                "exportSessionMessageCountCacheMap": {
                    scope_key: {"counts": {"wxid_demo": 3}}
                },
                "contactsListCacheMap": {
                    scope_key: {
                        "contacts": [
                            {"username": "wxid_demo", "displayName": "Demo"}
                        ]
                    }
                },
                "exportAutomationTaskMap": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "taskId": "pkas-weflow-manual-test",
                "sessionIds": ["wxid_demo", "missing"],
                "start": "2026-09-01 00:00",
                "end": "2026-09-06 12:00",
                "lastWatermarkMs": 1_700_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["WEFLOW_ROOT"] = str(weflow_root)
    environment["WEFLOW_CONFIG_DIR"] = str(config_dir)

    configured = subprocess.run(  # noqa: S603
        [str(node), str(helper), "configure", str(request_path)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert configured.returncode == 0, configured.stderr
    configured_payload = json.loads(configured.stdout)
    assert configured_payload["session_count"] == 1
    assert configured_payload["secret_fields_accessed"] is False
    configured_store = json.loads(
        (config_dir / "WeFlow-config.json").read_text(encoding="utf-8")
    )
    configured_task = configured_store["exportAutomationTaskMap"][scope_key]["tasks"][0]
    assert configured_task["condition"]["type"] == "manual-range-replay"

    audited = subprocess.run(  # noqa: S603
        [str(node), str(helper), "audit", "none"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert audited.returncode == 0, audited.stderr
    assert json.loads(audited.stdout)["enabled_task_count"] == 1

    cleaned = subprocess.run(  # noqa: S603
        [str(node), str(helper), "cleanup", "pkas-weflow-manual-test"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert cleaned.returncode == 0, cleaned.stderr
    assert json.loads(cleaned.stdout)["removed"] is True
