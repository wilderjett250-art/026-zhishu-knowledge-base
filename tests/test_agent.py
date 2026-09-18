import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from langsmith.run_helpers import get_tracing_context
from pydantic import SecretStr

from pkas.agent import AgentService
from pkas.agent_worker import run_agent_jobs
from pkas.config import Settings
from pkas.llm import DeepSeekGateway, LLMBudgetExceededError
from pkas.project_state import ProjectInspector
from pkas.prompts import CLOSEOUT, PLANNER, PROMPT_REGISTRY, SYNTHESIS
from pkas.system import KnowledgeSystem

CODEX_USER_TASK_METADATA = {
    "record_kind": "user_task",
    "assistant_output_indexed": False,
    "evidence_basis": "user_request_only",
}


def deepseek_response(content: dict[str, Any], *, input_tokens: int = 120) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        "usage": {
            "prompt_cache_hit_tokens": 20,
            "prompt_cache_miss_tokens": input_tokens - 20,
            "completion_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


def configured_settings(test_settings: Settings) -> Settings:
    return Settings(
        project_root=test_settings.project_root,
        data_root=test_settings.data_root,
        allowed_origins=[],
        deepseek_api_key=SecretStr("test-only-key"),
    )


def test_knowledge_system_agent_uses_shared_retrieval_service(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = KnowledgeSystem.create(test_settings)
    system.ingestion.import_text(
        text="统一混合检索会保留来源定位。",
        title="统一检索测试",
        original_uri="test://unified-retrieval",
        source_type="txt",
        domain="work",
        privacy="private",
    )
    calls: list[str] = []
    original_search = system.retrieval.search

    def tracked_search(query: str, **kwargs: Any):
        calls.append(query)
        return original_search(query, **kwargs)

    monkeypatch.setattr(system.retrieval, "search", tracked_search)

    tool_result = system.agent.tools.search_knowledge.invoke(
        {"query": "统一混合检索", "domain": "work", "limit": 5}
    )
    context_result = system.agent.prepare_context(
        task="统一混合检索",
        domain="work",
        limit=5,
        include_restricted=False,
    )

    assert calls == ["统一混合检索", "统一混合检索"]
    assert tool_result["retrieval"]["mode"].startswith("fts")
    assert tool_result["items"][0]["original_uri"] == "test://unified-retrieval"
    stored_run = system.repository.get_agent_run(context_result["run_id"])
    assert stored_run is not None
    assert stored_run["context"]["retrieval"]["mode"].startswith("fts")


def test_deepseek_gateway_uses_flash_non_thinking_and_application_cache(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    requests: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return deepseek_response({"summary": "缓存测试"})

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    first = gateway.complete_json(
        task_type="test",
        system_prompt="请输出 JSON 对象。",
        payload={"task": "测试"},
        prompt_version="test-v1",
    )
    second = gateway.complete_json(
        task_type="test",
        system_prompt="请输出 JSON 对象。",
        payload={"task": "测试"},
        prompt_version="test-v1",
    )

    assert len(requests) == 1
    assert requests[0]["model"] == "deepseek-v4-flash"
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert first.application_cache_hit is False
    assert first.usage["prompt_cache_miss_tokens"] == 100
    assert second.application_cache_hit is True
    assert second.usage["output_tokens"] == 0
    usage = system.repository.llm_usage_since("2000-01-01")
    assert usage["cache_miss_tokens"] == 100
    assert usage["output_tokens"] == 40


def test_deepseek_gateway_retries_empty_json_once(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    responses = [
        {
            "choices": [{"message": {"content": ""}}],
            "usage": {},
        },
        deepseek_response({"summary": "第二次有效"}),
    ]
    calls = 0

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return responses.pop(0)

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    result = gateway.complete_json(
        task_type="retry-test",
        system_prompt="输出 JSON。",
        payload={"task": "重试"},
        use_cache=False,
    )

    assert calls == 2
    assert result.content["summary"] == "第二次有效"


def test_agent_prompts_are_versioned_grounded_and_injection_aware() -> None:
    assert set(PROMPT_REGISTRY) == {
        "knowledge-retrieval-planner",
        "grounded-knowledge-synthesis",
        "codex-task-closeout",
    }
    assert PLANNER.version == "retrieval-plan-v2"
    assert SYNTHESIS.version == "evidence-synthesis-v3-project-scope"
    assert CLOSEOUT.version == "codex-closeout-v5-project-scope"
    assert "不可信证据" in PLANNER.system_prompt
    assert "evidence_ids" in SYNTHESIS.system_prompt
    assert "测试通过不等于部署" in CLOSEOUT.system_prompt
    assert "只代表“要求做什么”" in CLOSEOUT.system_prompt
    assert SYNTHESIS.max_tokens < 1200
    assert (PLANNER.min_tokens, SYNTHESIS.min_tokens, CLOSEOUT.min_tokens) == (200, 700, 300)


def test_deepseek_gateway_repairs_semantically_invalid_json_once(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    responses = [
        deepseek_response({"summary": "缺少当前状态"}),
        deepseek_response({"summary": "已修正", "current_state": "证据充分"}),
    ]
    requests: list[dict[str, Any]] = []

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        requests.append(payload)
        return responses.pop(0)

    def validate(content: dict[str, Any]) -> dict[str, Any]:
        if not content.get("summary") or not content.get("current_state"):
            raise ValueError("summary 和 current_state 必填")
        return content

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    result = gateway.complete_json(
        task_type="semantic-repair-test",
        system_prompt="只输出 JSON。",
        payload={"task": "测试"},
        prompt_version="semantic-repair-v1",
        validator=validate,
        validation_hint="补齐必填字段。",
        use_cache=False,
    )

    assert result.content["current_state"] == "证据充分"
    assert len(requests) == 2
    assert len(requests[1]["messages"]) == 4
    assert "未通过字段校验" in requests[1]["messages"][-1]["content"]
    usage = system.repository.llm_usage_since("2000-01-01")
    assert usage["cache_miss_tokens"] == 200


def test_deepseek_gateway_clamps_output_to_remaining_daily_budget(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings).model_copy(
        update={
            "agent_daily_output_token_budget": 1_000,
            "agent_min_output_tokens_per_call": 128,
        }
    )
    system = KnowledgeSystem.create(settings)
    system.repository.record_llm_call(
        agent_run_id=None,
        task_type="budget-seed",
        request_hash="budget-seed",
        model=settings.deepseek_flash_model,
        thinking_mode="disabled",
        status="completed",
        usage={
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "output_tokens": 800,
            "reasoning_tokens": 0,
        },
    )
    observed_max_tokens = 0

    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal observed_max_tokens
        observed_max_tokens = int(payload["max_tokens"])
        return deepseek_response({"summary": "预算内完成"})

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    result = gateway.complete_json(
        task_type="budget-clamp-test",
        system_prompt="只输出 JSON。",
        payload={"task": "测试"},
        max_tokens=500,
        use_cache=False,
    )

    assert result.content["summary"] == "预算内完成"
    assert observed_max_tokens == 200


def test_deepseek_gateway_respects_prompt_minimum_before_calling_api(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings).model_copy(
        update={"agent_daily_output_token_budget": 1_000}
    )
    system = KnowledgeSystem.create(settings)
    system.repository.record_llm_call(
        agent_run_id=None,
        task_type="budget-seed",
        request_hash="minimum-seed",
        model=settings.deepseek_flash_model,
        thinking_mode="disabled",
        status="completed",
        usage={
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "output_tokens": 500,
            "reasoning_tokens": 0,
        },
    )
    called = False

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return deepseek_response({"summary": "不应调用"})

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
    )
    with pytest.raises(LLMBudgetExceededError):
        gateway.complete_json(
            task_type="minimum-budget-test",
            system_prompt="只输出 JSON。",
            payload={"task": "测试"},
            max_tokens=1_000,
            min_tokens=700,
            use_cache=False,
        )
    assert called is False


def test_deepseek_gateway_counts_invalid_json_usage_before_retry(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    responses = [
        {
            "choices": [{"message": {"content": "{"}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 30},
        },
        deepseek_response({"summary": "修复成功"}, input_tokens=120),
    ]

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return responses.pop(0)

    result = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
        sleeper=lambda _seconds: None,
    ).complete_json(
        task_type="invalid-json-usage-test",
        system_prompt="只输出 JSON。",
        payload={"task": "测试"},
        use_cache=False,
    )

    assert result.content["summary"] == "修复成功"
    usage = system.repository.llm_usage_since("2000-01-01")
    assert usage["cache_miss_tokens"] == 180
    assert usage["output_tokens"] == 70


def test_agent_plans_retrieves_synthesizes_and_persists_knowledge(
    test_settings: Settings,
    source_root: Path,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    note = source_root / "pkas-status.md"
    note.write_text(
        "PKAS 已完成 Codex 历史同步，DeepSeek Agent 正在接入。",
        encoding="utf-8",
    )
    system.workflows.run_import(
        path=str(note),
        recursive=False,
        domain="work",
        privacy="private",
    )
    responses = [
        deepseek_response(
            {
                "search_queries": ["PKAS Codex 历史同步 DeepSeek Agent"],
                "catalog_queries": [],
                "plan": ["检索项目状态", "综合证据", "返回下一步"],
            }
        ),
        deepseek_response(
            {
                "summary": "知识库底座已可用，Agent 接入进行中。",
                "current_state": "Codex 历史同步已有证据，DeepSeek Agent 尚未完成验收。",
                "completed": ["Codex 历史同步"],
                "remaining": ["Agent 端到端验收"],
                "risks": ["尚未完成真实 API 调用"],
                "next_actions": ["完成 DeepSeek API 验收"],
                "confidence": "high",
            }
        ),
    ]
    tracing_states: list[bool | str | None] = []

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        tracing_states.append(get_tracing_context()["enabled"])
        return responses.pop(0)

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    agent = AgentService(
        system.repository,
        settings=settings,
        gateway=gateway,
    )
    result = agent.run(
        task="告诉我 PKAS 当前 Codex 同步和 DeepSeek Agent 进度",
        domain="work",
        persist_result=True,
    )

    assert result["status"] == "completed"
    assert tracing_states == [False, False]
    assert result["framework"] == "langgraph"
    assert result["checkpoint"]["next_nodes"] == []
    assert result["checkpoint"]["history_count"] >= 7
    assert result["result"]["evidence_count"] >= 1
    assert result["result"]["knowledge_item"]["review_status"] == "candidate"
    assert system.repository.stats()["counts"]["knowledge_items"] == 1
    runs = system.repository.list_agent_runs()
    assert runs[0]["status"] == "completed"
    assert settings.agent_checkpoint_path.exists()
    with system.database.connect() as connection:
        steps = connection.execute(
            """
            SELECT sequence, action_name, status
            FROM agent_steps WHERE run_id = ? ORDER BY sequence
            """,
            (result["run_id"],),
        ).fetchall()
    assert [row["sequence"] for row in steps] == [1, 2, 3, 4, 5, 6]
    assert steps[3]["action_name"] == "execute_langchain_tools"


def test_langgraph_agent_resumes_from_failed_deepseek_node(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    empty_response = {"choices": [{"message": {"content": ""}}], "usage": {}}
    responses = [
        empty_response,
        empty_response,
        deepseek_response(
            {
                "search_queries": ["resume checkpoint"],
                "catalog_queries": [],
                "plan": ["resume planning", "retrieve evidence", "synthesize"],
            }
        ),
        deepseek_response(
            {
                "summary": "The resumed graph completed.",
                "current_state": "The failed model node resumed from its checkpoint.",
                "completed": ["checkpoint resume"],
                "remaining": [],
                "risks": [],
                "next_actions": ["continue normal operation"],
                "confidence": "high",
            }
        ),
    ]

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return responses.pop(0)

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=transport,
        sleeper=lambda _seconds: None,
    )
    agent = AgentService(system.repository, settings=settings, gateway=gateway)

    first = agent.run(task="resume a failed knowledge graph", persist_result=False)

    assert first["status"] == "warning"
    assert first["error"]["code"] == "empty_content"
    status = agent.graph_status(first["run_id"])
    assert status["framework"] == "langgraph"
    assert status["resumable"] is True
    assert status["next_nodes"] == ["plan_retrieval"]

    resumed = agent.resume(first["run_id"])

    assert resumed["status"] == "completed"
    assert resumed["result"]["summary"] == "The resumed graph completed."
    assert agent.graph_status(first["run_id"])["resumable"] is False
    stored = system.repository.get_agent_run(first["run_id"])
    assert stored is not None
    assert stored["status"] == "completed"
    with system.database.connect() as connection:
        steps = connection.execute(
            "SELECT sequence, status FROM agent_steps WHERE run_id = ? ORDER BY sequence",
            (first["run_id"],),
        ).fetchall()
    assert [row["sequence"] for row in steps] == [1, 2, 3, 4, 5, 6]
    assert all(row["status"] in {"completed", "skipped"} for row in steps)


def test_project_inspector_reads_only_authorized_git_state(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    project = source_root / "project"
    project.mkdir()
    note = project / "README.md"
    note.write_text("first", encoding="utf-8")
    commands = [
        ["git", "init"],
        ["git", "config", "user.email", "pkas-test@example.invalid"],
        ["git", "config", "user.name", "PKAS Test"],
        ["git", "add", "README.md"],
        ["git", "commit", "-m", "initial"],
    ]
    for command in commands:
        subprocess.run(command, cwd=project, check=True, capture_output=True)
    knowledge_system.sync.register_root(
        name="测试项目",
        root_path=str(source_root),
        connector_type="local_files",
        sync_mode="catalog",
    )
    note.write_text("changed", encoding="utf-8")

    state = ProjectInspector(knowledge_system.repository).inspect(str(project))

    assert state["is_git_repository"] is True
    assert state["dirty"] is True
    assert state["changed_file_count"] == 1
    assert state["changed_files"][0]["path"] == "README.md"


def test_codex_task_request_cannot_be_promoted_to_completed(
    knowledge_system: KnowledgeSystem,
) -> None:
    guarded = knowledge_system.agent._guard_closeout_evidence(
        {
            "title": "订单同步已经完成",
            "summary": "文件已修改，测试和部署均已通过。",
            "completed": ["修改文件", "测试通过", "部署成功"],
            "remaining": [],
            "risks": [],
            "reusable_lessons": ["可以直接相信最终回答"],
            "evidence_ids": ["src_claim"],
            "confidence": "high",
        },
        [
            {
                "source_id": "src_claim",
                "source_type": "codex-turn",
                "title": "订单同步",
                "evidence_status": "user_request_only",
            }
        ],
    )

    assert guarded["completed"] == []
    assert guarded["confidence"] == "low"
    assert guarded["evidence_status"] == "user_request_only"
    assert guarded["reusable_lessons"] == []
    assert guarded["remaining"] == [
        "缺少独立机器证据，不能确认：修改文件",
        "缺少独立机器证据，不能确认：测试通过",
        "缺少独立机器证据，不能确认：部署成功",
    ]


def test_knowledge_synthesis_cannot_promote_codex_only_claims(
    knowledge_system: KnowledgeSystem,
) -> None:
    guarded = knowledge_system.agent._guard_synthesis_evidence(
        {
            "summary": "部署已经完成。",
            "current_state": "生产环境正常。",
            "completed": ["生产部署"],
            "remaining": [],
            "risks": [],
            "next_actions": [],
            "evidence_ids": ["src_claim"],
            "confidence": "high",
        },
        [
            {
                "source_id": "src_claim",
                "document_id": "doc_claim",
                "evidence_status": "user_request_only",
            }
        ],
    )

    assert guarded["completed"] == []
    assert guarded["confidence"] == "low"
    assert guarded["evidence_status"] == "user_request_only"
    assert guarded["remaining"] == ["缺少独立机器证据，不能确认：生产部署"]


def test_closeout_without_machine_evidence_does_not_create_knowledge_candidate(
    test_settings: Settings,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    imported = system.ingestion.import_text(
        text="# Codex 任务记录\n\n## 用户请求\n\n请修复订单同步并运行测试",
        title="订单同步任务",
        original_uri="codex://closeout/user-task-only",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata=CODEX_USER_TASK_METADATA,
    )

    gateway = DeepSeekGateway(
        settings=settings,
        repository=system.repository,
        transport=lambda _payload: deepseek_response(
            {
                "title": "订单同步已完成",
                "summary": "修改和测试都已完成。",
                "completed": ["修改订单同步", "测试通过"],
                "remaining": [],
                "risks": [],
                "reusable_lessons": ["直接相信任务描述"],
                "evidence_ids": [imported["source_id"]],
                "confidence": "high",
            }
        ),
        sleeper=lambda _seconds: None,
    )
    agent = AgentService(system.repository, settings=settings, gateway=gateway)

    result = agent.closeout_codex_turn(source_id=imported["source_id"])

    assert result["status"] == "completed"
    assert result["result"]["completed"] == []
    assert result["result"]["knowledge_item"] is None
    assert result["result"]["persistence_status"] == (
        "skipped_without_independent_evidence"
    )
    assert system.repository.stats()["counts"]["knowledge_items"] == 0


def test_agent_worker_claims_and_completes_queued_closeout(
    test_settings: Settings,
    monkeypatch,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    imported = system.ingestion.import_text(
        text="Codex 任务已经完成目标检查。",
        title="Agent worker test",
        original_uri="codex://worker/test",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata=CODEX_USER_TASK_METADATA,
    )
    job = system.repository.enqueue_agent_job(
        job_type="codex_closeout",
        source_id=imported["source_id"],
        workspace_path=None,
    )
    monkeypatch.setattr(
        system.agent,
        "closeout_codex_turn",
        lambda **_kwargs: {"status": "completed", "run_id": "agr_test", "result": {}},
    )

    report = run_agent_jobs(system, max_jobs=1, job_id=job["id"])

    assert report["status"] == "completed"
    assert report["completed"] == 1
    assert system.repository.list_agent_jobs()[0]["status"] == "completed"


def test_agent_worker_processes_daily_closeout_batch(
    test_settings: Settings,
    monkeypatch,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    first = system.ingestion.import_text(
        text="已修改第一个文件。",
        title="Daily batch one",
        original_uri="codex://daily/thread/one",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata=CODEX_USER_TASK_METADATA,
    )
    second = system.ingestion.import_text(
        text="测试通过。",
        title="Daily batch two",
        original_uri="codex://daily/thread/two",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata=CODEX_USER_TASK_METADATA,
    )
    source_ids = [first["source_id"], second["source_id"]]
    job = system.repository.enqueue_agent_job(
        job_type="codex_daily_closeout",
        source_id=second["source_id"],
        workspace_path=None,
        payload={"source_ids": source_ids, "dedupe_key": "daily-batch-test"},
    )
    received: dict[str, Any] = {}

    def complete_batch(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {"status": "completed", "run_id": "agr_daily", "result": {}}

    monkeypatch.setattr(system.agent, "closeout_codex_batch", complete_batch)

    report = run_agent_jobs(system, max_jobs=1, job_id=job["id"])

    assert report["status"] == "completed"
    assert received["source_ids"] == source_ids
    assert system.repository.list_agent_jobs()[0]["status"] == "completed"


def test_agent_worker_defers_budget_without_consuming_attempt(
    test_settings: Settings,
    monkeypatch,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    imported = system.ingestion.import_text(
        text="已修改知识库。",
        title="Budget defer",
        original_uri="codex://worker/budget-defer",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata=CODEX_USER_TASK_METADATA,
    )
    job = system.repository.enqueue_agent_job(
        job_type="codex_closeout",
        source_id=imported["source_id"],
        workspace_path=None,
    )
    monkeypatch.setattr(
        system.agent,
        "closeout_codex_turn",
        lambda **_kwargs: {
            "status": "warning",
            "run_id": "agr_budget_defer",
            "error": {
                "code": "daily_token_budget_exceeded",
                "message": "budget",
                "retryable": False,
            },
        },
    )

    report = run_agent_jobs(system, max_jobs=1, job_id=job["id"])

    stored = system.repository.list_agent_jobs()[0]
    assert report["deferred"] == 1
    assert stored["status"] == "pending"
    assert stored["attempts"] == 0


def test_agent_worker_resumes_checkpoint_on_retry(
    test_settings: Settings,
    monkeypatch,
) -> None:
    settings = configured_settings(test_settings)
    system = KnowledgeSystem.create(settings)
    imported = system.ingestion.import_text(
        text="Codex closeout retry evidence.",
        title="Agent resume worker test",
        original_uri="codex://worker/resume-test",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata=CODEX_USER_TASK_METADATA,
    )
    job = system.repository.enqueue_agent_job(
        job_type="codex_closeout",
        source_id=imported["source_id"],
        workspace_path=None,
    )
    calls = {"closeout": 0, "resume": 0}

    def fail_closeout(**_kwargs: Any) -> dict[str, Any]:
        calls["closeout"] += 1
        return {
            "status": "warning",
            "run_id": "agr_resume_worker",
            "error": {
                "code": "temporary_network_error",
                "message": "temporary",
                "retryable": True,
            },
        }

    def complete_resume(run_id: str) -> dict[str, Any]:
        calls["resume"] += 1
        assert run_id == "agr_resume_worker"
        return {"status": "completed", "run_id": run_id, "result": {}}

    monkeypatch.setattr(system.agent, "closeout_codex_turn", fail_closeout)
    monkeypatch.setattr(system.agent, "resume", complete_resume)

    first = run_agent_jobs(system, max_jobs=1, job_id=job["id"])
    second = run_agent_jobs(system, max_jobs=1, job_id=job["id"])

    assert first["status"] == "warning"
    assert first["pending"] == 1
    assert second["status"] == "completed"
    assert second["completed"] == 1
    assert calls == {"closeout": 1, "resume": 1}
