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
