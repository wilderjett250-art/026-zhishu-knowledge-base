import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.thread_journal import ThreadJournalService


def write_session(
    path: Path,
    *,
    assistant_text: str = "助手说已经完成",
    duplicate: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-09-10T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": "thread-demo", "cwd": r"E:\demo"},
        },
        {
            "timestamp": "2026-09-10T01:01:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "请修复同步，并验证测试。"}],
            },
        },
        {
            "timestamp": "2026-09-10T01:02:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text}],
            },
        },
    ]
    if duplicate:
        rows.append(
            {
                "timestamp": "2026-09-10T01:03:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "请修复同步，并验证测试。"}
                    ],
                },
            }
        )
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


class FakeAgent:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def complete(self, packet):
        FakeAgent.calls += 1
        return (
            {
                "items": [
                    {
                        "id": session["id"],
                        "title": "同步修复任务",
                        "summary": "用户要求修复同步并验证测试。",
                        "requirements": ["修复同步", "验证测试"],
                        "decisions": [],
                        "lessons": [],
                        "open_items": ["完成状态仍需机器证据"],
                        "uncertainties": [],
                        "evidence_message_ids": ["m1"],
                    }
                    for session in packet["sessions"]
                ]
            },
            "agent-thread",
        )


def test_conversation_digest_is_full_first_then_file_incremental(knowledge_system):
    knowledge_system.database.initialize()
    session_path = (
        knowledge_system.settings.client_home
        / ".codex"
        / "sessions"
        / "2026"
        / "09"
        / "demo.jsonl"
    )
    write_session(session_path)
    FakeAgent.calls = 0
    service = ThreadJournalService(
        knowledge_system.settings,
        knowledge_system.repository,
        agent_factory=FakeAgent,
    )
    first = service.refresh(now=datetime(2026, 9, 11, tzinfo=UTC))
    assert first["scan"]["changed"] == 1
    assert first["summaries_written"] == 1
    assert first["pending_files"] == 0
    assert FakeAgent.calls == 1
    summary = next(service.output_root.glob("*.md")).read_text(encoding="utf-8")
    assert "自动合并完全重复：1 条" in summary
    assert "助手说已经完成" not in summary
    assert "完成判断：未验证" in summary

    second = service.refresh(now=datetime(2026, 9, 18, tzinfo=UTC))
    assert second["scan"]["stat_unchanged"] == 1
    assert second["summaries_written"] == 0
    assert FakeAgent.calls == 1

    write_session(session_path, duplicate=False)
    with session_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-09-18T01:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "增加回归检查。"}
                        ],
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    third = service.refresh(
        now=datetime(2026, 9, 18, 2, tzinfo=UTC), force_scan=True
    )
    assert third["scan"]["changed"] == 1
    assert third["summaries_written"] == 1
    assert FakeAgent.calls == 2


def test_thread_journal_api_queues_without_waiting_for_model(test_settings):
    with TestClient(create_app(test_settings)) as client:
        status = client.get("/api/thread-journal/status")
        assert status.status_code == 200
        assert status.json()["data"]["enabled"] is False
        refreshed = client.post("/api/thread-journal/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["data"]["status"] in {"queued", "running"}
