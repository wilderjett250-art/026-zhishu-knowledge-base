import json
from pathlib import Path

from openpyxl import Workbook, load_workbook

from pkas.system import KnowledgeSystem

CHATLAB_FIXTURE = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"


def copy_chatlab_fixture(source_root: Path) -> Path:
    target = source_root / "weflow-customer.json"
    target.write_bytes(CHATLAB_FIXTURE.read_bytes())
    return target


def create_xlsx_export_fixture(source_root: Path) -> tuple[Path, Path]:
    xlsx = source_root / "测试客户张经理-聊天记录.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["微信聊天记录"])
    sheet.append(["导出工具", "WeFlow", "导出版本", "test"])
    sheet.append(["昵称", "测试客户张经理", "微信ID", "wxid_customer_demo"])
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
            "2025-02-05 09:20:00",
            "张经理",
            "wxid_customer_demo",
            "客户-张经理",
            "客户",
            "文本消息",
            "门店系统希望本周五前看到报价单。",
        ]
    )
    sheet.append(
        [
            2,
            "2025-02-05 09:21:00",
            "我",
            "wxid_owner",
            "",
            "我",
            "文本消息",
            "收到，我会先核对接口范围再给您正式报价。",
        ]
    )
    sheet.append(
        [
            3,
            "2025-02-05 09:22:00",
            "张经理",
            "wxid_customer_demo",
            "客户-张经理",
            "客户",
            "文本消息",
            "还需要包含三家门店的数据看板。",
        ]
    )
    workbook.save(xlsx)
    records = source_root / "weflow-export-records.json"
    records.write_text(
        json.dumps(
            {
                "wxid_customer_demo": [
                    {
                        "exportTime": 1738713900000,
                        "format": "xlsx",
                        "messageCount": 3,
                        "outputPath": str(xlsx.resolve()),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return records, xlsx


def test_weflow_xlsx_export_discovery_import_deduplication_and_context(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    records, _ = create_xlsx_export_fixture(source_root)
    catalog = knowledge_system.weflow.discover_exports(records_path=str(records))
    assert catalog["api_required"] is False
    assert catalog["key_accessed"] is False
    assert catalog["total_sessions"] == 1
    assert catalog["existing_sessions"] == 1
    assert catalog["existing_record_count"] == 1

    inspection = knowledge_system.weflow.inspect_export_selection(
        records_path=str(records),
        session_ids=["wxid_customer_demo"],
    )
    assert inspection["selected_sessions"] == 1
    assert inspection["total_messages"] == 3
    assert inspection["items"][0]["display_name"] == "测试客户张经理"
    assert inspection["items"][0]["header_row"] == 5

    imported = knowledge_system.customer_workflows.import_weflow_exports(
        records_path=str(records),
        session_ids=["wxid_customer_demo"],
        inspection_token=inspection["inspection_token"],
        privacy="restricted",
    )
    assert imported["status"] == "completed"
    assert imported["result"]["imported"] == 3
    snapshot = Path(imported["result"]["snapshot_paths"][0])
    assert snapshot.is_file()
    assert snapshot.suffix == ".xlsx"

    customers = knowledge_system.customers.list_customers()
    assert len(customers) == 1
    customer = customers[0]
    assert customer["display_name"] == "测试客户张经理"
    assert customer["message_count"] == 3
    assert knowledge_system.customers.timeline(customer["id"]) == []
    timeline = knowledge_system.customers.timeline(
        customer["id"],
        include_restricted=True,
    )
    assert len(timeline) == 3
    assert timeline[0]["content"] == "还需要包含三家门店的数据看板。"
    assert timeline[0]["local_id"] == "xlsx-row:8"
    assert timeline[1]["is_self"] == 1
    assert timeline[0]["source_uri"].endswith("测试客户张经理-聊天记录.xlsx")

    results = knowledge_system.customers.search_messages(
        "三家门店 数据看板",
        customer_id=customer["id"],
        include_restricted=True,
    )
    assert len(results) == 1
    context = knowledge_system.customer_service.prepare_reply_context(
        customer_id=customer["id"],
        task="回复客户关于门店数据看板和报价的问题",
        include_restricted=True,
    )
    assert context is not None
    assert len(context["recent_messages"]) == 3

    duplicate = knowledge_system.customer_workflows.import_weflow_exports(
        records_path=str(records),
        session_ids=["wxid_customer_demo"],
        inspection_token=inspection["inspection_token"],
        privacy="restricted",
    )
    assert duplicate["result"]["imported"] == 0
    assert duplicate["result"]["duplicates"] == 3

    with knowledge_system.database.connect() as connection:
        connector = connection.execute(
            "SELECT config_json FROM connectors WHERE connector_type = 'weflow-xlsx-export'"
        ).fetchone()
    assert '"api_required":false' in connector["config_json"]
    assert '"key_accessed":false' in connector["config_json"]


def test_weflow_xlsx_inspection_token_detects_file_change(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    records, xlsx = create_xlsx_export_fixture(source_root)
    inspection = knowledge_system.weflow.inspect_export_selection(
        records_path=str(records),
        session_ids=["wxid_customer_demo"],
    )
    workbook = load_workbook(xlsx)
    sheet = workbook.active
    assert sheet is not None
    sheet.append(
        [
            4,
            "2025-02-05 09:23:00",
            "张经理",
            "wxid_customer_demo",
            "客户-张经理",
            "客户",
            "文本消息",
            "检查后新增的消息",
        ]
    )
    workbook.save(xlsx)

    result = knowledge_system.customer_workflows.import_weflow_exports(
        records_path=str(records),
        session_ids=["wxid_customer_demo"],
        inspection_token=inspection["inspection_token"],
        privacy="restricted",
    )
    assert result["status"] == "failed"
    assert "发生了变化" in result["error"]["message"]


def test_offline_chatlab_import_remains_supported(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    chatlab = copy_chatlab_fixture(source_root)
    inspection = knowledge_system.weflow.inspect_chatlab_file(str(chatlab))
    assert inspection["session_id"] == "wxid_customer_demo"
    assert inspection["message_count"] == 3

    imported = knowledge_system.customer_workflows.import_chatlab(
        path=str(chatlab),
        inspection_token=inspection["inspection_token"],
        session_id=None,
        privacy="restricted",
    )
    assert imported["status"] == "completed"
    assert imported["result"]["imported"] == 3


def test_chatlab_inspection_token_detects_file_change(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    chatlab = copy_chatlab_fixture(source_root)
    inspection = knowledge_system.weflow.inspect_chatlab_file(str(chatlab))
    payload = json.loads(chatlab.read_text(encoding="utf-8"))
    payload["messages"].append(
        {
            "sender": "wxid_customer_demo",
            "timestamp": 1738714000,
            "type": 0,
            "content": "检查后新增的消息",
            "platformMessageId": "wf-demo-004",
        }
    )
    chatlab.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = knowledge_system.customer_workflows.import_chatlab(
        path=str(chatlab),
        inspection_token=inspection["inspection_token"],
        session_id=None,
        privacy="restricted",
    )
    assert result["status"] == "failed"
    assert "发生了变化" in result["error"]["message"]
