from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pkas.system import KnowledgeSystem
from pkas.weflow_manual_worker import _failure_code

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
