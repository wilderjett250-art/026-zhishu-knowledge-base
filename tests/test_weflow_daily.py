import json
from pathlib import Path

from openpyxl import Workbook

from pkas.system import KnowledgeSystem
from pkas.weflow_daily import (
    authorize_daily_import,
    daily_import_is_enabled,
    disable_daily_import,
    run_daily_import,
)


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
    records, _ = _create_daily_export(source_root, export_time=500)
    authorization = authorize_daily_import(
        knowledge_system,
        records_path=str(records),
        authorized_at_ms=1000,
    )
    assert authorization["status"] == "authorized"
    assert authorization["privacy"] == "restricted"
    assert daily_import_is_enabled(knowledge_system) is True

    initial = run_daily_import(knowledge_system)
    assert initial["status"] == "completed"
    assert initial["candidate_sessions"] == 0

    payload = json.loads(records.read_text(encoding="utf-8"))
    payload["wxid_daily_demo"][0]["exportTime"] = 2000
    records.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    imported = run_daily_import(knowledge_system)
    assert imported["status"] == "completed"
    assert imported["candidate_sessions"] == 1
    assert imported["inspected_sessions"] == 1
    assert imported["imported_messages"] == 1
    assert imported["failed_sessions"] == 0

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
