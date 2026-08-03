from pathlib import Path

import pkas.mcp_server as mcp_server
from pkas.system import KnowledgeSystem


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

    imported = mcp_server.import_confirmed_path(
        str(note),
        domain="work",
        privacy="private",
        recursive=False,
        confirmed=True,
    )
    assert imported["status"] == "success"

    results = mcp_server.search_knowledge("证据定位", domain="work")
    assert results["status"] == "success"
    assert len(results["data"]) == 1
    assert results["data"][0]["original_uri"] == str(note.resolve())


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

    customers = mcp_server.list_weflow_customers()
    assert customers["status"] == "success"
    customer_id = imported["customer_id"]

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
