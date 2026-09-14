import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import pkas.mcp_server as mcp_server
from pkas.codex_capture import capture_notification
from pkas.daily_closeout import plan_daily_closeouts
from pkas.ingest import ImportBoundaryError
from pkas.repository import CODEX_USER_TASK_MIGRATION_KEY, Repository
from pkas.sync_worker import run_sync
from pkas.system import KnowledgeSystem


def test_codex_notify_capture_redacts_and_deduplicates(
    knowledge_system: KnowledgeSystem,
) -> None:
    synthetic_api_key = "sk-" + ("a" * 32)
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "thread-notify",
        "turn-id": "turn-1",
        "cwd": "E:\\workproject\\example",
        "input-messages": [{"content": "请记住 api_key=very-secret-value"}],
        "last-assistant-message": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    }
    payload["input-messages"].append({"content": f"raw key: {synthetic_api_key}"})

    first = capture_notification(payload, ingestion=knowledge_system.ingestion)
    second = capture_notification(payload, ingestion=knowledge_system.ingestion)

    assert first["status"] == "imported"
    assert second["status"] == "duplicate"
    assert knowledge_system.repository.list_agent_jobs() == []
    document = knowledge_system.repository.read_document(first["document_id"])
    assert document is not None
    assert document["source_type"] == "codex-turn"
    assert document["evidence_status"] == "user_request_only"
    assert document["metadata"]["record_kind"] == "user_task"
    assert document["metadata"]["assistant_output_indexed"] is False
    assert document["metadata"]["evidence_basis"] == "user_request_only"
    assert "Codex 最终回答" not in document["text"]
    assert "very-secret-value" not in document["text"]
    assert synthetic_api_key not in document["text"]
    assert "abcdefghijklmnopqrstuvwxyz" not in document["text"]
    assert document["text"].count("[REDACTED]") >= 2


def test_ingestion_rejects_codex_assistant_output(
    knowledge_system: KnowledgeSystem,
) -> None:
    with pytest.raises(ImportBoundaryError, match="助手回答不得进入知识库"):
        knowledge_system.ingestion.import_text(
            text="## 用户请求\n\n修复问题\n\n## Codex 最终回答\n\n已经完成。",
            title="invalid Codex record",
            original_uri="codex://invalid/assistant-output",
            source_type="codex-turn",
            domain="work",
            privacy="private",
            metadata={"record_kind": "assistant_claim"},
        )


def test_legacy_codex_records_are_rewritten_to_user_tasks(
    knowledge_system: KnowledgeSystem,
) -> None:
    legacy_text = (
        "# Codex 任务记录\n\n"
        "- thread_id：legacy-thread\n"
        "- turn_id：legacy-turn\n\n"
        "## 用户请求\n\n"
        "请修复历史迁移问题\n\n"
        "## Codex 最终回答（未验证的助手声明）\n\n"
        "蓝色火箭验收完成，所有测试通过。"
    )
    imported = knowledge_system.ingestion.import_text(
        text=legacy_text,
        title="legacy Codex record",
        original_uri="codex://legacy/thread/turn",
        source_type="legacy-codex-turn",
        domain="work",
        privacy="private",
        metadata={"record_kind": "assistant_claim", "assistant_output_indexed": True},
    )
    knowledge_system.ingestion.import_text(
        text="普通资料在 FTS 重建后仍然可以检索。",
        title="normal source",
        original_uri="pkas://test/normal-source",
        source_type="text",
        domain="work",
        privacy="private",
    )
    candidate = knowledge_system.repository.create_knowledge_candidate(
        domain="work",
        knowledge_type="project_state",
        title="错误完成候选",
        content="蓝色火箭已经验收完成。",
        confidence="high",
        privacy="private",
        evidence_ids=[imported["source_id"]],
    )
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE sources SET source_type = 'codex-turn' WHERE id = ?",
            (imported["source_id"],),
        )
        connection.execute(
            "DELETE FROM app_meta WHERE key IN (?, ?)",
            (CODEX_USER_TASK_MIGRATION_KEY, f"{CODEX_USER_TASK_MIGRATION_KEY}_result"),
        )
        connection.commit()

    repository = Repository(knowledge_system.database)
    migration = repository.run_codex_turn_user_task_migration()
    assert migration["user_tasks_migrated"] == 1
    document = repository.source_document(imported["source_id"])
    assert document is not None
    assert "请修复历史迁移问题" in document["text_content"]
    assert "蓝色火箭验收完成" not in document["text_content"]
    assert document["metadata"]["record_kind"] == "user_task"
    assert document["metadata"]["assistant_output_indexed"] is False
    assert "蓝色火箭验收完成" not in Path(imported["vault_path"]).read_text(encoding="utf-8")
    assert repository.search("请修复历史迁移问题", domain="work")
    assert repository.search("蓝色火箭验收完成", domain="work") == []
    assert repository.search("普通资料", domain="work")
    invalidated = next(
        item for item in repository.list_knowledge_items() if item["id"] == candidate["id"]
    )
    assert invalidated["review_status"] == "rejected"
    assert "蓝色火箭" not in invalidated["content"]


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


def test_codex_notify_capture_ignores_internal_title_generation(
    knowledge_system: KnowledgeSystem,
) -> None:
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "title-thread",
        "turn-id": "title-turn",
        "cwd": "E:\\wordspace\\7.26",
        "input-messages": [
            {
                "content": (
                    "You are a helpful assistant. You will be presented with a user prompt, "
                    "and your job is to provide a short title for a task."
                )
            }
        ],
        "last-assistant-message": "Short title",
    }

    result = capture_notification(payload, ingestion=knowledge_system.ingestion)

    assert result == {"status": "ignored", "reason": "internal_codex_turn"}
    assert knowledge_system.repository.list_sources() == []


def test_codex_notify_capture_ignores_internal_activity_update(
    knowledge_system: KnowledgeSystem,
) -> None:
    payload = {
        "type": "agent-turn-complete",
        "thread-id": "activity-thread",
        "turn-id": "activity-turn",
        "cwd": "E:\\wordspace\\7.26",
        "input-messages": [
            {
                "content": (
                    "You write the one-line activity update displayed beneath an existing "
                    "Codex task title."
                )
            }
        ],
        "last-assistant-message": '{"summary":"已完成全部修改"}',
    }

    result = capture_notification(payload, ingestion=knowledge_system.ingestion)

    assert result == {"status": "ignored", "reason": "internal_codex_turn"}
    assert knowledge_system.repository.list_sources() == []


def test_codex_turn_indexes_user_request_but_never_assistant_output(
    knowledge_system: KnowledgeSystem,
) -> None:
    imported = capture_notification(
        {
            "type": "agent-turn-complete",
            "thread-id": "claim-thread",
            "turn-id": "claim-turn",
            "cwd": "E:\\workproject\\claim",
            "input-messages": [{"content": "修复订单同步"}],
            "last-assistant-message": "紫色独角兽验收完成，测试全部通过。",
        },
        ingestion=knowledge_system.ingestion,
    )

    results = knowledge_system.repository.search("修复订单同步", limit=5)
    result = next(item for item in results if item["source_id"] == imported["source_id"])

    assert result["source_type"] == "codex-turn"
    assert result["evidence_status"] == "user_request_only"
    assert "只记录用户" in result["evidence_warning"]
    assert knowledge_system.repository.search("紫色独角兽验收完成", limit=5) == []


def test_database_initialization_supersedes_historical_activity_update(
    knowledge_system: KnowledgeSystem,
) -> None:
    imported = knowledge_system.ingestion.import_text(
        text=(
            "You write the one-line activity update displayed beneath an existing "
            "Codex task title.\n\n已完成全部修改。"
        ),
        title="internal activity update",
        original_uri="codex://internal/activity-existing",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata={"record_kind": "user_task", "assistant_output_indexed": False},
    )

    knowledge_system.database.initialize()

    with knowledge_system.database.connect() as connection:
        status = connection.execute(
            "SELECT status FROM sources WHERE id = ?",
            (imported["source_id"],),
        ).fetchone()["status"]
    assert status == "superseded"
    assert knowledge_system.repository.search("one-line activity update", domain="work") == []


def test_daily_closeout_groups_material_thread_and_consolidates_legacy_jobs(
    knowledge_system: KnowledgeSystem,
) -> None:
    material = capture_notification(
        {
            "type": "agent-turn-complete",
            "thread-id": "daily-thread",
            "turn-id": "daily-turn-1",
            "cwd": "E:\\workproject\\daily",
            "input-messages": [{"content": "修复同步问题"}],
            "last-assistant-message": "已修改同步代码，测试通过。",
        },
        ingestion=knowledge_system.ingestion,
    )
    capture_notification(
        {
            "type": "agent-turn-complete",
            "thread-id": "daily-thread",
            "turn-id": "daily-turn-2",
            "cwd": "E:\\workproject\\daily",
            "input-messages": [{"content": "解释一下原因"}],
            "last-assistant-message": "原因是游标没有持久化。",
        },
        ingestion=knowledge_system.ingestion,
    )
    capture_notification(
        {
            "type": "agent-turn-complete",
            "thread-id": "question-thread",
            "turn-id": "question-turn-1",
            "cwd": "E:\\workproject\\daily",
            "input-messages": [{"content": "什么是全文检索"}],
            "last-assistant-message": "全文检索用于查找文本。",
        },
        ingestion=knowledge_system.ingestion,
    )
    legacy = knowledge_system.repository.enqueue_agent_job(
        job_type="codex_closeout",
        source_id=material["source_id"],
        workspace_path="E:\\workproject\\daily",
        payload={"capture_mode": "notify"},
    )
    legacy_daily = knowledge_system.repository.enqueue_agent_job(
        job_type="codex_daily_closeout",
        source_id=material["source_id"],
        workspace_path="E:\\workproject\\daily",
        payload={"capture_mode": "daily", "source_ids": [material["source_id"]]},
    )

    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    first = plan_daily_closeouts(knowledge_system, now=cutoff)
    second = plan_daily_closeouts(
        knowledge_system,
        now=cutoff + timedelta(seconds=1),
    )

    assert first["turns_seen"] == 3
    assert first["thread_groups"] == 2
    assert first["queued"] == 1
    assert first["skipped_non_material"] == 1
    assert first["consolidated_legacy_jobs"] == 1
    assert first["consolidated_legacy_daily_jobs"] == 1
    assert second["turns_seen"] == 0
    assert second["queued"] == 0
    jobs = knowledge_system.repository.list_agent_jobs()
    daily_job = next(job for job in jobs if job["job_type"] == "codex_daily_closeout")
    legacy_job = next(job for job in jobs if job["id"] == legacy["id"])
    legacy_daily_job = next(job for job in jobs if job["id"] == legacy_daily["id"])
    assert daily_job["status"] == "pending"
    assert daily_job["payload"]["capture_mode"] == "daily-batch-v2"
    assert len(daily_job["payload"]["source_ids"]) == 2
    assert legacy_job["status"] == "skipped"
    assert legacy_daily_job["status"] == "skipped"


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
        _event(
            "event_msg",
            {"type": "task_started", "turn_id": "turn-internal-activity"},
        ),
        _event(
            "event_msg",
            {
                "type": "user_message",
                "message": (
                    "You write the one-line activity update displayed beneath an existing "
                    "Codex task title."
                ),
            },
        ),
        _event(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "turn-internal-activity",
                "last_agent_message": '{"summary":"已完成全部修改"}',
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
    history_results = knowledge_system.repository.search("增量同步", domain="work")
    assert history_results
    assert history_results[0]["evidence_status"] == "user_request_only"
    history_document = knowledge_system.repository.read_document(
        history_results[0]["document_id"]
    )
    assert history_document is not None
    assert history_document["metadata"]["evidence_basis"] == "user_request_only"
    assert history_document["metadata"]["assistant_output_indexed"] is False
    assert "按游标读取新增字节" not in history_document["text"]
    assert knowledge_system.repository.search("工具私密输出", domain="work") == []
    assert knowledge_system.repository.search("one-line activity update", domain="work") == []

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
    assert (
        len(
            knowledge_system.repository.search(
                "第二轮只读",
                domain="work",
                include_unverified_claims=True,
            )
        )
        == 1
    )


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
