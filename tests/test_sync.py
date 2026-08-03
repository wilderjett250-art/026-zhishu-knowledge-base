import json
from pathlib import Path

import pkas.mcp_server as mcp_server
from pkas.codex_capture import capture_notification
from pkas.sync_worker import run_sync
from pkas.system import KnowledgeSystem


def test_codex_notify_capture_redacts_and_deduplicates(
    knowledge_system: KnowledgeSystem,
) -> None:
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "thread-notify",
        "turn-id": "turn-1",
        "cwd": "E:\\workproject\\example",
        "input-messages": [{"content": "请记住 api_key=very-secret-value"}],
        "last-assistant-message": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    }

    first = capture_notification(payload, ingestion=knowledge_system.ingestion)
    second = capture_notification(payload, ingestion=knowledge_system.ingestion)

    assert first["status"] == "imported"
    assert second["status"] == "duplicate"
    document = knowledge_system.repository.read_document(first["document_id"])
    assert document is not None
    assert "very-secret-value" not in document["text"]
    assert "abcdefghijklmnopqrstuvwxyz" not in document["text"]
    assert document["text"].count("[REDACTED]") >= 2


def test_codex_notify_capture_ignores_desktop_ambient_suggestion(
    knowledge_system: KnowledgeSystem,
) -> None:
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "ambient-thread",
        "turn-id": "ambient-turn",
        "cwd": (
            "C:\\Program Files\\WindowsApps\\"
            "OpenAI.Codex_26.727.6591.0_x64__2p2nqsd0c76g0\\app"
        ),
        "input-messages": [
            {
                "content": (
                    "You are an expert at upholding safety and compliance standards "
                    "for Codex ambient suggestions."
                )
            }
        ],
        "last-assistant-message": "Internal suggestion output",
    }

    result = capture_notification(payload, ingestion=knowledge_system.ingestion)

    assert result == {"status": "ignored", "reason": "internal_codex_turn"}
    assert knowledge_system.repository.list_sources() == []


def _event(event_type: str, payload: dict[str, object]) -> str:
    return json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False)


def test_codex_session_streaming_is_incremental_and_ignores_tool_output(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    sessions = source_root / "sessions"
    sessions.mkdir()
    session_file = sessions / "session-1.jsonl"
    lines = [
        _event(
            "session_meta",
            {"id": "thread-history", "cwd": "E:\\workproject\\history"},
        ),
        _event("event_msg", {"type": "thread_name_updated", "thread_name": "历史任务"}),
        _event(
            "event_msg",
            {"type": "task_started", "turn_id": "turn-history-1", "started_at": "t1"},
        ),
        _event("event_msg", {"type": "user_message", "message": "如何做增量同步？"}),
        _event(
            "response_item",
            {"type": "function_call_output", "output": "工具私密输出不应入库"},
        ),
        _event(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "turn-history-1",
                "last_agent_message": "按游标读取新增字节。",
                "completed_at": "t2",
            },
        ),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    root = knowledge_system.sync.register_root(
        name="Codex 历史任务",
        root_path=str(sessions),
        connector_type="codex_sessions",
        sync_mode="index",
        privacy="private",
    )

    first = knowledge_system.sync.scan_root(root["id"])
    second = knowledge_system.sync.scan_root(root["id"])
    assert first["turns_indexed"] == 1
    assert second["turns_indexed"] == 0
    assert second["unchanged"] == 1
    assert knowledge_system.repository.search("增量同步", domain="work")
    assert knowledge_system.repository.search("工具私密输出", domain="work") == []

    appended = [
        _event("event_msg", {"type": "task_started", "turn_id": "turn-history-2"}),
        _event("event_msg", {"type": "user_message", "message": "第二轮只读新增内容"}),
        _event(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "turn-history-2",
                "last_agent_message": "不会重新扫描旧字节。",
            },
        ),
    ]
    with session_file.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(appended) + "\n")
    third = knowledge_system.sync.scan_root(root["id"])
    assert third["turns_indexed"] == 1
    assert len(knowledge_system.repository.search("第二轮只读", domain="work")) == 1


def test_local_sync_catalogs_skips_secrets_and_supersedes_changed_content(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "business.md"
    note.write_text("业务规则第一版：先看旧证据。", encoding="utf-8")
    (source_root / ".env").write_text("TOKEN=never-index", encoding="utf-8")
    root = knowledge_system.sync.register_root(
        name="业务资料",
        root_path=str(source_root),
        connector_type="local_files",
        sync_mode="index",
        privacy="private",
    )

    first = knowledge_system.sync.scan_root(root["id"])
    assert first["indexed"] == 1
    assert first["skipped"] == 1
    old_result = knowledge_system.repository.search("先看旧证据", domain="work")
    assert len(old_result) == 1

    note.write_text("业务规则第二版：先看当前证据并验证。", encoding="utf-8")
    second = knowledge_system.sync.scan_root(root["id"])
    assert second["indexed"] == 1
    assert knowledge_system.repository.search("先看旧证据", domain="work") == []
    assert len(knowledge_system.repository.search("当前证据并验证", domain="work")) == 1
    statuses = {item["status"] for item in knowledge_system.repository.list_sources()}
    assert statuses == {"indexed", "superseded"}
    stats = knowledge_system.repository.stats()
    assert stats["counts"]["sources"] == 1
    assert stats["counts"]["chunks"] == 1


def test_mcp_sync_requires_confirmation(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mcp_server, "system", lambda: knowledge_system)
    (source_root / "catalog.txt").write_text("资料目录测试", encoding="utf-8")

    blocked = mcp_server.register_sync_root("目录", str(source_root))
    assert blocked["status"] == "warning"
    assert knowledge_system.sync.list_roots() == []

    registered = mcp_server.register_sync_root(
        "目录",
        str(source_root),
        sync_mode="catalog",
        confirmed=True,
    )
    root_id = registered["data"]["id"]
    scanned = mcp_server.scan_sync_root(root_id)
    assert scanned["status"] == "success"
    results = mcp_server.search_source_catalog("catalog")
    assert len(results["data"]) == 1


def test_sync_worker_refreshes_authorized_roots_and_writes_report(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    (source_root / "worker.txt").write_text("后台增量同步", encoding="utf-8")
    knowledge_system.sync.register_root(
        name="后台同步测试",
        root_path=str(source_root),
        connector_type="local_files",
        sync_mode="catalog",
    )

    report = run_sync(knowledge_system, connector_type="local_files")

    assert report["status"] == "completed"
    assert report["selected_roots"] == 1
    assert Path(report["report_path"]).is_file()
    assert knowledge_system.sync.search_catalog("worker.txt")
