import io
from collections import deque
from pathlib import Path

import pytest

from pkas.summary_agent import AgentError, CodexAgent


def test_summary_workers_do_not_reenter_history_sync():
    from pkas.codex_capture import is_internal_codex_turn

    for prompt in ("PKAS_SUMMARY_AGENT_V1", "你是用户私人知识库的文件整理助手"):
        assert is_internal_codex_turn(user_text=prompt, cwd="I:/data/summary-jobs/123/agent-work")
        assert not is_internal_codex_turn(user_text=prompt, cwd="E:/ordinary-project")


def test_dedicated_agent_disables_inherited_notify(monkeypatch, source_root):
    captured = []

    class Process:
        stdin = io.BytesIO()
        stdout = io.BytesIO()

        def poll(self):
            return 0

    monkeypatch.setattr("pkas.summary_agent.codex_binary", lambda _: source_root / "codex.exe")
    monkeypatch.setattr(
        "pkas.summary_agent.subprocess.Popen",
        lambda args, **kwargs: captured.append(args) or Process(),
    )
    monkeypatch.setattr(CodexAgent, "rpc", lambda *args, **kwargs: {})
    with CodexAgent(source_root):
        pass
    assert "notify=[]" in captured[0]


def test_summary_agent_resumes_its_recorded_thread_without_creating_another():
    agent = CodexAgent.__new__(CodexAgent)
    agent.model = "test"
    agent.cwd = Path("I:/test-summary-agent")
    agent.instructions = "Only analyze current packet"
    agent.output_schema = {"type": "object"}
    agent.thread_id = None
    agent._resume_thread_id = "thread-1"
    agent.events = deque()
    agent.models = lambda: [{"model": "test"}]
    calls = []

    def rpc(method, params, timeout=30):
        calls.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": "thread-1"}}
        assert method == "turn/start"
        agent.events.extend([
            {"method": "item/completed", "params": {
                "threadId": "thread-1", "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": '{"items": []}'},
            }},
            {"method": "turn/completed", "params": {
                "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"},
            }},
        ])
        return {"turn": {"id": "turn-1"}}

    agent.rpc = rpc
    saved = []
    output, ident = agent.summarize({"items": []}, saved.append)
    assert output == {"items": []}
    assert ident == "thread-1"
    assert saved == ["thread-1"]
    assert [method for method, _ in calls] == ["thread/resume", "turn/start"]
    assert calls[0][1]["config"]["mcp_servers"] == {}
    assert calls[0][1]["approvalPolicy"] == "never"


def test_summary_agent_rejects_wrong_resumed_thread_id():
    agent = CodexAgent.__new__(CodexAgent)
    agent.model = "test"
    agent.cwd = Path("I:/test-summary-agent")
    agent.instructions = "Only analyze current packet"
    agent.output_schema = {"type": "object"}
    agent.thread_id = None
    agent._resume_thread_id = "thread-expected"
    agent.events = deque()
    agent.models = lambda: [{"model": "test"}]
    calls = []

    def rpc(method, params, timeout=30):
        calls.append(method)
        return {"thread": {"id": "thread-different"}}

    agent.rpc = rpc
    with pytest.raises(AgentError, match="任务编号不符"):
        agent.summarize({"items": []})
    assert calls == ["thread/resume"]


def test_fast_turn_events_before_rpc_ack_are_not_lost():
    agent = CodexAgent.__new__(CodexAgent)
    agent.model = "test"
    agent.thread_id = "thread-1"
    agent.sequence = 0
    agent.events = deque()
    agent.send = lambda value: None
    events = iter(
        [
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "text": '{"items": []}'},
                },
            },
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
            },
            {"id": 1, "result": {"turn": {"id": "turn-1"}}},
        ]
    )
    agent.next = lambda deadline: next(events)
    output, thread = agent.summarize({"items": []})
    assert output == {"items": []}
    assert thread == "thread-1"


def test_failed_turn_keeps_only_safe_provider_category():
    agent = CodexAgent.__new__(CodexAgent)
    agent.model = "test"
    agent.thread_id = "thread-1"
    agent.sequence = 0
    agent.events = deque()
    agent.send = lambda value: None
    events = iter(
        [
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "failed",
                        "error": {
                            "message": "do not persist this raw provider message",
                            "codexErrorInfo": {
                                "type": "UsageLimitExceeded", "httpStatusCode": 429
                            },
                        },
                    },
                },
            },
            {"id": 1, "result": {"turn": {"id": "turn-1"}}},
        ]
    )
    agent.next = lambda deadline: next(events)
    with pytest.raises(AgentError) as exc:
        agent.summarize({"items": []})
    assert exc.value.code == "UsageLimitExceeded:http_429"
    assert "provider message" not in str(exc.value)


def test_error_event_category_is_used_when_final_turn_omits_error():
    agent = CodexAgent.__new__(CodexAgent)
    agent.model = "test"
    agent.thread_id = "thread-1"
    agent.sequence = 0
    agent.events = deque()
    agent.send = lambda value: None
    events = iter(
        [
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "error": {
                        "message": "never persist this detail",
                        "codexErrorInfo": {
                            "type": "InternalServerError", "httpStatusCode": 500
                        },
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "failed"},
                },
            },
            {"id": 1, "result": {"turn": {"id": "turn-1"}}},
        ]
    )
    agent.next = lambda deadline: next(events)
    with pytest.raises(AgentError) as exc:
        agent.summarize({"items": []})
    assert exc.value.code == "InternalServerError:http_500"
