import json
from pathlib import Path

from openpyxl import Workbook

import pkas.mcp_server as mcp_server
from pkas.codex_capture import capture_notification
from pkas.system import KnowledgeSystem


def test_mcp_tool_annotations_match_side_effects() -> None:
    tools = {item.name: item for item in mcp_server.mcp._tool_manager.list_tools()}

    for name in {
        "search_knowledge",
        "read_document",
        "list_sources",
        "inspect_import_path",
        "list_sync_roots",
        "search_source_catalog",
        "discover_weflow_exports",
        "inspect_weflow_exports",
        "list_weflow_customers",
        "get_customer_timeline",
        "search_customer_messages",
        "list_agent_jobs",
        "get_agent_token_usage",
        "get_agent_graph_status",
    }:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.open_world_hint is False

    for name in {
        "import_confirmed_path",
        "register_sync_root",
        "scan_sync_root",
        "import_confirmed_weflow_exports",
        "prepare_agent_context",
        "save_persona_candidate",
        "save_distillation_candidate",
        "prepare_customer_reply_context",
        "save_customer_signal_candidate",
    }:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert annotations.open_world_hint is False

    for name in {"run_knowledge_agent", "run_codex_closeout", "resume_agent_run"}:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert annotations.open_world_hint is True


def test_mcp_guard_and_search(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mcp_server, "system", lambda: knowledge_system)
    note = source_root / "agent.txt"
    note.write_text("智能体回答前必须检索知识库并保留证据定位。", encoding="utf-8")

    blocked = mcp_server.import_confirmed_path(str(note), confirmed=False)
    assert blocked["status"] == "warning"
    assert knowledge_system.repository.stats()["counts"]["sources"] == 0

    inspection = mcp_server.inspect_import_path(str(note), recursive=False)
    imported = mcp_server.import_confirmed_path(
        str(note),
        inspection_token=inspection["data"]["inspection_token"],
        domain="work",
        privacy="private",
        recursive=False,
        confirmed=True,
    )
    assert imported["status"] == "success"

    results = mcp_server.search_knowledge("证据定位", domain="work")
    assert results["status"] == "warning"
    assert results["retrieval"]["mode"] == "fts_only"
    assert len(results["data"]) == 1
    assert results["data"][0]["original_uri"] == str(note.resolve())

    capture_notification(
        {
            "type": "agent-turn-complete",
            "thread-id": "mcp-user-task",
            "turn-id": "mcp-user-task-1",
            "input-messages": [{"content": "调查隐蔽部署现状"}],
            "last-assistant-message": "绿色彗星部署已经完成。",
        },
        ingestion=knowledge_system.ingestion,
    )
    user_task = mcp_server.search_knowledge("调查隐蔽部署现状", domain="work")
    assistant_output = mcp_server.search_knowledge(
        "绿色彗星部署已经完成",
        domain="work",
        include_unverified_claims=True,
    )
    assert user_task["data"] == []  # Shared file scope excludes task-request records.
    assert assistant_output["data"] == []


def test_mcp_customer_context_tools(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mcp_server, "system", lambda: knowledge_system)
    fixture = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"
    chatlab = source_root / "mcp-customer.json"
    chatlab.write_bytes(fixture.read_bytes())
    inspection = knowledge_system.weflow.inspect_chatlab_file(str(chatlab))
    imported = knowledge_system.weflow.import_chatlab_file(
        path=str(chatlab),
        inspection_token=inspection["inspection_token"],
    )

    customer_id = imported["customer_id"]
    customers = mcp_server.list_weflow_customers()
    assert customers["status"] == "success"
    assert customers["data"] == []
    unreviewed = mcp_server.list_weflow_customers(include_candidates=True)
    assert len(unreviewed["data"]) == 1

    blocked_context = mcp_server.prepare_customer_reply_context(
        customer_id,
        "回复客户报价问题",
        include_restricted=True,
    )
    assert blocked_context["status"] == "warning"
    approved = knowledge_system.customers.update_customer(
        customer_id,
        review_status="approved",
    )
    assert approved is not None
    assert len(mcp_server.list_weflow_customers()["data"]) == 1

    hidden = mcp_server.search_customer_messages("报价单", customer_id=customer_id)
    visible = mcp_server.search_customer_messages(
        "报价单",
        customer_id=customer_id,
        include_restricted=True,
    )
    assert hidden["data"] == []
    assert len(visible["data"]) == 1

    context = mcp_server.prepare_customer_reply_context(
        customer_id,
        "回复客户报价问题",
        include_restricted=True,
    )
    assert context["status"] == "success"
    assert len(context["data"]["recent_messages"]) == 3

    signal = mcp_server.save_customer_signal_candidate(
        customer_id,
        "commitment",
        "需要回复正式报价时间。",
        evidence_message_ids=[visible["data"][0]["message_id"]],
        confidence="high",
    )
    assert signal["data"]["approval_status"] == "candidate"


def test_mcp_weflow_export_inspection_and_confirmed_import(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mcp_server, "system", lambda: knowledge_system)
    xlsx = source_root / "weflow-mcp.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["微信聊天记录"])
    sheet.append(["昵称", "MCP 测试客户", "微信ID", "wxid_mcp_customer"])
    sheet.append([])
    sheet.append(
        ["序号", "时间", "发送者昵称", "发送者微信ID", "发送者身份", "消息类型", "内容"]
    )
    sheet.append(
        [1, "2026-08-03 09:00:00", "客户", "wxid_mcp_customer", "客户", "文本消息", "测试需求"]
    )
    workbook.save(xlsx)
    records = source_root / "weflow-export-records.json"
    records.write_text(
        json.dumps(
            {
                "wxid_mcp_customer": [
                    {
                        "exportTime": 1785720000000,
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

    inspection = mcp_server.inspect_weflow_exports(
        records_path=str(records),
        all_existing=True,
    )
    assert inspection["status"] == "success"
    assert inspection["data"]["selected_sessions"] == 1

    blocked = mcp_server.import_confirmed_weflow_exports(
        inspection["data"]["inspection_token"],
        records_path=str(records),
        all_existing=True,
    )
    assert blocked["status"] == "warning"
    assert knowledge_system.repository.stats()["counts"]["customer_messages"] == 0

    imported = mcp_server.import_confirmed_weflow_exports(
        inspection["data"]["inspection_token"],
        records_path=str(records),
        all_existing=True,
        confirmed=True,
    )
    assert imported["status"] == "success"
    assert imported["data"]["result"]["imported"] == 1
