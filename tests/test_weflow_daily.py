import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

from pkas.system import KnowledgeSystem
from pkas.weflow_daily import (
    authorize_daily_import,
    daily_import_is_enabled,
    disable_daily_import,
    run_daily_import,
)
from pkas.weflow_retention import MIN_AGE_MS, prune_verified_exports

EXPORT_WATERMARK_MS = 1_789_547_138_000


def _create_daily_export(source_root: Path, *, export_time: int) -> tuple[Path, Path]:
    xlsx = source_root / "daily-chat.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["微信聊天记录"])
    sheet.append(["导出工具", "WeFlow", "导出版本", "test"])
    sheet.append(["昵称", "测试会话", "微信ID", "wxid_daily_demo"])
    sheet.append([])
    sheet.append(
        [
            "序号",
            "时间",
            "发送者昵称",
            "发送者微信ID",
            "发送者备注",
            "发送者身份",
            "消息类型",
            "内容",
        ]
    )
    sheet.append(
        [
            1,
            "2026-08-24 09:00:00",
            "测试会话",
            "wxid_daily_demo",
            "",
            "对方",
            "文本消息",
            "今天的新消息",
        ]
    )
    workbook.save(xlsx)
    records = source_root / "weflow-export-records.json"
    records.write_text(
        json.dumps(
            {
                "wxid_daily_demo": [
                    {
                        "exportTime": export_time,
                        "format": "xlsx",
                        "messageCount": 1,
                        "outputPath": str(xlsx.resolve()),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return records, xlsx


def test_daily_import_starts_after_authorization_and_deduplicates(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    records, _ = _create_daily_export(
        source_root, export_time=EXPORT_WATERMARK_MS - 500
    )
    authorization = authorize_daily_import(
        knowledge_system,
        records_path=str(records),
        authorized_at_ms=EXPORT_WATERMARK_MS,
    )
    assert authorization["status"] == "authorized"
    assert authorization["privacy"] == "restricted"
    assert daily_import_is_enabled(knowledge_system) is True

    initial = run_daily_import(knowledge_system)
    assert initial["status"] == "completed"
    assert initial["candidate_sessions"] == 0

    payload = json.loads(records.read_text(encoding="utf-8"))
    # A newer export in the *same second* must not be lost to seconds truncation.
    payload["wxid_daily_demo"][0]["exportTime"] = EXPORT_WATERMARK_MS + 200
    records.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    imported = run_daily_import(knowledge_system)
    assert imported["status"] == "completed"
    assert imported["candidate_sessions"] == 1
    assert imported["inspected_sessions"] == 1
    assert imported["imported_messages"] == 1
    assert imported["failed_sessions"] == 0
    state = json.loads(
        (knowledge_system.settings.data_root / "config" / "weflow-daily-import.json")
        .read_text(encoding="utf-8")
    )
    assert state["last_completed_export_time_ms"] == EXPORT_WATERMARK_MS + 200

    unchanged = run_daily_import(knowledge_system)
    assert unchanged["status"] == "completed"
    assert unchanged["candidate_sessions"] == 0


def test_daily_import_can_be_disabled(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    records, _ = _create_daily_export(source_root, export_time=2000)
    authorize_daily_import(
        knowledge_system,
        records_path=str(records),
        authorized_at_ms=1000,
    )
    disabled = disable_daily_import(knowledge_system)
    assert disabled["enabled"] is False
    assert daily_import_is_enabled(knowledge_system) is False
    result = run_daily_import(knowledge_system)
    assert result["status"] == "disabled"
    assert result["candidate_sessions"] == 0


def test_daily_import_low_disk_keeps_watermark_for_retry(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    records, _ = _create_daily_export(
        source_root, export_time=EXPORT_WATERMARK_MS + 200
    )
    authorize_daily_import(
        knowledge_system,
        records_path=str(records),
        authorized_at_ms=EXPORT_WATERMARK_MS,
    )
    monkeypatch.setattr(
        "pkas.weflow_daily.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1024),
    )

    result = run_daily_import(knowledge_system)

    assert result["status"] == "failed"
    assert result["error_code"] == "low_disk_space"
    assert result["candidate_sessions"] == 1
    state = json.loads(
        (knowledge_system.settings.data_root / "config" / "weflow-daily-import.json")
        .read_text(encoding="utf-8")
    )
    assert state["last_completed_export_time_ms"] == EXPORT_WATERMARK_MS


def test_managed_export_retention_requires_verified_vault_and_keeps_latest_two(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    managed = source_root / "PKAS-WeFlow-Exports"
    managed.mkdir()
    monkeypatch.setenv("PKAS_WEFLOW_EXPORT_ROOT", str(managed))
    now_ms = EXPORT_WATERMARK_MS + 20 * 24 * 60 * 60 * 1000
    old = managed / "old.xlsx"
    middle = managed / "middle.xlsx"
    newest = managed / "newest.xlsx"
    outside = source_root / "user-export.xlsx"
    for path, content in ((old, b"old"), (middle, b"middle"),
                          (newest, b"newest"), (outside, b"outside")):
        path.write_bytes(content)
    digest = hashlib.sha256(old.read_bytes()).hexdigest()
    vault = (knowledge_system.settings.data_root / "raw" / "weflow-xlsx"
             / "sha256" / digest[:2] / f"{digest}.xlsx")
    vault.parent.mkdir(parents=True)
    vault.write_bytes(old.read_bytes())
    with sqlite3.connect(knowledge_system.settings.database_path) as connection:
        connection.execute(
            """INSERT INTO connector_snapshots
               (id, connector_id, conversation_id, content_hash, vault_path,
                byte_size, source_uri, captured_at, metadata_json)
               VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, '{}')""",
            ("snapshot-old", digest, str(vault), old.stat().st_size,
             str(old), "2026-09-01T00:00:00+00:00"),
        )
    records = source_root / "records.json"
    records.write_text(json.dumps({"session-a": [
        {"exportTime": now_ms - 12 * 86400000, "outputPath": str(old)},
        {"exportTime": now_ms - 11 * 86400000, "outputPath": str(middle)},
        {"exportTime": now_ms - 10 * 86400000, "outputPath": str(newest)},
        {"exportTime": now_ms - 13 * 86400000, "outputPath": str(outside)},
    ]}), encoding="utf-8")
    result = prune_verified_exports(
        records_path=records,
        database_path=knowledge_system.settings.database_path,
        data_root=knowledge_system.settings.data_root,
        watermark_ms=now_ms,
        now_ms=now_ms,
    )
    assert result == {"pruned_files": 1, "freed_bytes": 3, "retention_errors": 0}
    assert not old.exists()
    assert middle.is_file() and newest.is_file() and outside.is_file()
    assert vault.is_file()


def test_managed_export_retention_does_not_delete_without_vault(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    managed = source_root / "PKAS-WeFlow-Exports"
    managed.mkdir()
    monkeypatch.setenv("PKAS_WEFLOW_EXPORT_ROOT", str(managed))
    files = [managed / f"part-{number}.xlsx" for number in range(3)]
    for number, path in enumerate(files):
        path.write_bytes(f"payload-{number}".encode())
    now_ms = EXPORT_WATERMARK_MS + MIN_AGE_MS + 86400000
    records = source_root / "records.json"
    records.write_text(json.dumps({"session-a": [
        {"exportTime": EXPORT_WATERMARK_MS + index,
         "outputPath": str(path)} for index, path in enumerate(files)
    ]}), encoding="utf-8")
    result = prune_verified_exports(
        records_path=records,
        database_path=knowledge_system.settings.database_path,
        data_root=knowledge_system.settings.data_root,
        watermark_ms=now_ms,
        now_ms=now_ms,
    )
    assert result["pruned_files"] == 0
    assert all(path.is_file() for path in files)
