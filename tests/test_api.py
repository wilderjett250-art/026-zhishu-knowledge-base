import json
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from pkas.api import create_app
from pkas.config import Settings

WEFLOW_FIXTURE = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"


def create_weflow_xlsx_export(source_root: Path) -> Path:
    xlsx = source_root / "api-customer.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["微信聊天记录"])
    sheet.append(["昵称", "API 测试客户", "微信ID", "wxid_api_customer"])
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
            "2025-02-05 10:00:00",
            "客户",
            "wxid_api_customer",
            "",
            "客户",
            "文本消息",
            "请提供报价。",
        ]
    )
    sheet.append(
        [2, "2025-02-05 10:01:00", "我", "wxid_owner", "", "我", "文本消息", "收到。"]
    )
    workbook.save(xlsx)
    records = source_root / "weflow-export-records.json"
    records.write_text(
        json.dumps(
            {
                "wxid_api_customer": [
                    {
                        "exportTime": 1738749660000,
                        "format": "xlsx",
                        "messageCount": 2,
                        "outputPath": str(xlsx.resolve()),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return records


def test_api_end_to_end(test_settings: Settings, source_root: Path) -> None:
    note = source_root / "customer.txt"
    note.write_text("客户要求所有结论都附带原始证据路径。", encoding="utf-8")

    with TestClient(create_app(test_settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "success"

        inspection = client.post(
            "/api/import/inspect",
            json={"path": str(note), "recursive": False},
        )
        assert inspection.status_code == 200
        inspection_data = inspection.json()["data"]
        assert inspection_data["supported_files"] == 1

        imported = client.post(
            "/api/import/run",
            json={
                "path": str(note),
                "recursive": False,
                "domain": "work",
                "privacy": "private",
                "inspection_token": inspection_data["inspection_token"],
            },
        )
        assert imported.status_code == 200
        assert imported.json()["data"]["result"]["imported"] == 1

        searched = client.post(
            "/api/search",
            json={"query": "原始证据路径", "domain": "work", "limit": 10},
        )
        result = searched.json()["data"][0]
        assert result["original_uri"] == str(note.resolve())

        document = client.get(f"/api/documents/{result['document_id']}")
        assert document.status_code == 200
        assert "所有结论" in document.json()["data"]["text"]

        context = client.post(
            "/api/agent/context",
            json={"task": "根据客户要求整理报告", "limit": 8},
        )
        assert context.status_code == 200
        assert context.json()["data"]["selected_domain"] == "work"

        dashboard = client.get("/api/dashboard").json()["data"]
        assert dashboard["counts"]["sources"] == 1
        assert dashboard["counts"]["workflow_runs"] == 1
        assert dashboard["counts"]["agent_runs"] == 1


def test_api_rejects_knowledge_project_as_import_source(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/import/inspect",
            json={"path": str(test_settings.project_root), "recursive": True},
        )
    assert response.status_code == 400
    assert "不能作为导入来源" in response.json()["detail"]


def test_api_requires_current_inspection_token(
    test_settings: Settings,
    source_root: Path,
) -> None:
    note = source_root / "changing.txt"
    note.write_text("第一次检查", encoding="utf-8")

    with TestClient(create_app(test_settings)) as client:
        inspection = client.post(
            "/api/import/inspect",
            json={"path": str(note), "recursive": False},
        ).json()["data"]
        note.write_text("检查后内容发生变化", encoding="utf-8")
        response = client.post(
            "/api/import/run",
            json={
                "path": str(note),
                "recursive": False,
                "domain": "work",
                "privacy": "private",
                "inspection_token": inspection["inspection_token"],
            },
        )
        dashboard = client.get("/api/dashboard").json()["data"]

    assert response.status_code == 200
    assert response.json()["status"] == "warning"
    assert "发生了变化" in response.json()["data"]["error"]["message"]
    assert dashboard["counts"]["sources"] == 0


def test_weflow_xlsx_export_api_flow(
    test_settings: Settings,
    source_root: Path,
) -> None:
    records = create_weflow_xlsx_export(source_root)

    with TestClient(create_app(test_settings)) as client:
        discovered = client.post(
            "/api/weflow/exports/discover",
            json={"records_path": str(records), "keyword": "", "limit": 100},
        )
        assert discovered.status_code == 200
        catalog = discovered.json()["data"]
        assert catalog["api_required"] is False
        assert catalog["existing_sessions"] == 1

        inspected = client.post(
            "/api/weflow/exports/inspect",
            json={
                "records_path": str(records),
                "session_ids": ["wxid_api_customer"],
            },
        )
        assert inspected.status_code == 200
        inspection = inspected.json()["data"]
        assert inspection["total_messages"] == 2

        imported = client.post(
            "/api/weflow/exports/import",
            json={
                "records_path": str(records),
                "session_ids": ["wxid_api_customer"],
                "inspection_token": inspection["inspection_token"],
                "privacy": "restricted",
            },
        )
        assert imported.status_code == 200
        assert imported.json()["data"]["result"]["imported"] == 2
        customers = client.get("/api/customers").json()["data"]
        assert customers[0]["display_name"] == "API 测试客户"


def test_weflow_customer_api_end_to_end(
    test_settings: Settings,
    source_root: Path,
) -> None:
    chatlab = source_root / "customer-chatlab.json"
    chatlab.write_bytes(WEFLOW_FIXTURE.read_bytes())

    with TestClient(create_app(test_settings)) as client:
        inspection_response = client.post(
            "/api/weflow/chatlab/inspect",
            json={"path": str(chatlab), "session_id": None},
        )
        assert inspection_response.status_code == 200
        inspection = inspection_response.json()["data"]

        imported = client.post(
            "/api/weflow/chatlab/import",
            json={
                "path": str(chatlab),
                "session_id": None,
                "inspection_token": inspection["inspection_token"],
                "privacy": "restricted",
            },
        )
        assert imported.status_code == 200
        assert imported.json()["data"]["result"]["imported"] == 3

        customer = client.get("/api/customers").json()["data"][0]
        updated = client.post(
            f"/api/customers/{customer['id']}",
            json={
                "company": "示例门店公司",
                "stage": "delivery",
                "tags": ["门店", "数据看板"],
                "summary": "客户正在确认三家门店的数据看板范围。",
                "review_status": "approved",
            },
        ).json()["data"]
        assert updated["company"] == "示例门店公司"
        assert updated["stage"] == "delivery"
        hidden = client.get(f"/api/customers/{customer['id']}/timeline").json()["data"]
        visible = client.get(
            f"/api/customers/{customer['id']}/timeline",
            params={"include_restricted": "true"},
        ).json()["data"]
        assert hidden == []
        assert len(visible) == 3

        searched = client.post(
            "/api/customer-messages/search",
            json={
                "query": "报价单",
                "customer_id": customer["id"],
                "include_restricted": True,
            },
        )
        evidence = searched.json()["data"][0]
        assert evidence["platform_message_id"] == "wf-demo-001"

        signal = client.post(
            "/api/customer-signals",
            json={
                "customer_id": customer["id"],
                "signal_type": "commitment",
                "statement": "需要回复正式报价时间。",
                "evidence_message_ids": [evidence["message_id"]],
                "confidence": "high",
            },
        ).json()["data"]
        reviewed = client.post(
            f"/api/customer-signals/{signal['id']}/review",
            json={"decision": "approved", "reason": "消息证据已核对"},
        )
        assert reviewed.status_code == 200

        context = client.post(
            "/api/customer-reply/context",
            json={
                "customer_id": customer["id"],
                "task": "回复报价和门店看板问题",
                "include_restricted": True,
            },
        )
        assert context.status_code == 200
        assert len(context.json()["data"]["recent_messages"]) == 3
