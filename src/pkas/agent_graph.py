import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from pkas.agent_errors import AgentValidationError
from pkas.codex_capture import redact_secrets
from pkas.llm import LLMError
from pkas.prompts import CLOSEOUT, PLANNER, SYNTHESIS

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from pkas.agent import AgentService


class KnowledgeAgentState(TypedDict):
    run_id: str
    graph_kind: str
    task: str
    workspace_path: str | None
    selected_domain: str | None
    include_restricted: bool
    persist_result: bool
    complexity: str
    project_state: dict[str, Any] | None
    initial_results: list[dict[str, Any]]
    planner: dict[str, Any]
    plan: list[str]
    evidence: dict[str, list[dict[str, Any]]]
    result: dict[str, Any]


class CloseoutAgentState(TypedDict):
    run_id: str
    graph_kind: str
    source_id: str
    source_ids: NotRequired[list[str]]
    workspace_path: str | None
    source_title: str
    project_state: dict[str, Any] | None
    closeout: dict[str, Any]
    model: str
    usage: dict[str, int]
    source_privacy: str
    result: dict[str, Any]


GraphKind = Literal["knowledge", "closeout"]


class AgentGraphRuntime:
    """LangGraph orchestration around PKAS business services and tools."""

    def __init__(
        self,
        host: "AgentService",
        *,
        planner_prompt: str,
        synthesis_prompt: str,
        closeout_prompt: str,
    ) -> None:
        self.host = host
        self.planner_prompt = planner_prompt
        self.synthesis_prompt = synthesis_prompt
        self.closeout_prompt = closeout_prompt

    def invoke_knowledge(self, state: KnowledgeAgentState) -> dict[str, Any]:
        run_id = state["run_id"]
        with tracing_context(enabled=False), self._compiled_graph("knowledge") as graph:
            return cast(dict[str, Any], graph.invoke(state, self._config(run_id, "knowledge")))

    def invoke_closeout(self, state: CloseoutAgentState) -> dict[str, Any]:
        run_id = state["run_id"]
        with tracing_context(enabled=False), self._compiled_graph("closeout") as graph:
            return cast(dict[str, Any], graph.invoke(state, self._config(run_id, "closeout")))

    def resume(self, run_id: str, graph_kind: GraphKind) -> dict[str, Any]:
        with tracing_context(enabled=False), self._compiled_graph(graph_kind) as graph:
            return cast(dict[str, Any], graph.invoke(None, self._config(run_id, graph_kind)))

    def status(self, run_id: str, graph_kind: GraphKind) -> dict[str, Any]:
        config = self._config(run_id, graph_kind)
        with tracing_context(enabled=False), self._compiled_graph(graph_kind) as graph:
            snapshot = graph.get_state(config)
            history_count = sum(1 for _ in graph.get_state_history(config, limit=100))
        configurable = snapshot.config.get("configurable", {})
        values = dict(snapshot.values) if snapshot.values else {}
        return {
            "framework": "langgraph",
            "graph_kind": graph_kind,
            "thread_id": configurable.get("thread_id", self._thread_id(run_id, graph_kind)),
            "checkpoint_id": configurable.get("checkpoint_id"),
            "next_nodes": list(snapshot.next),
            "resumable": bool(snapshot.next),
            "history_count": history_count,
            "state_keys": sorted(values),
            "values": values,
        }

    @contextmanager
    def _compiled_graph(self, graph_kind: GraphKind) -> Iterator["CompiledStateGraph"]:
        checkpoint_path = Path(self.host.settings.agent_checkpoint_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(checkpoint_path, timeout=30, check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        serializer = JsonPlusSerializer(allowed_msgpack_modules=[])
        checkpointer = SqliteSaver(connection, serde=serializer)
        checkpointer.setup()
        try:
            if graph_kind == "knowledge":
                yield self._build_knowledge_graph(checkpointer)
            else:
                yield self._build_closeout_graph(checkpointer)
        finally:
            connection.close()

    def _build_knowledge_graph(self, checkpointer: SqliteSaver) -> "CompiledStateGraph":
        builder = StateGraph(KnowledgeAgentState)
        builder.add_node("observe_project", self._knowledge_observe_project)
        builder.add_node("initial_retrieval", self._knowledge_initial_retrieval)
        builder.add_node("plan_retrieval", self._knowledge_plan_retrieval)
        builder.add_node("execute_retrieval", self._knowledge_execute_retrieval)
        builder.add_node("synthesize_evidence", self._knowledge_synthesize_evidence)
        builder.add_node("persist_candidate", self._knowledge_persist_candidate)
        builder.add_edge(START, "observe_project")
        builder.add_edge("observe_project", "initial_retrieval")
        builder.add_edge("initial_retrieval", "plan_retrieval")
        builder.add_edge("plan_retrieval", "execute_retrieval")
        builder.add_edge("execute_retrieval", "synthesize_evidence")
        builder.add_edge("synthesize_evidence", "persist_candidate")
        builder.add_edge("persist_candidate", END)
        return builder.compile(checkpointer=checkpointer)

    def _build_closeout_graph(self, checkpointer: SqliteSaver) -> "CompiledStateGraph":
        builder = StateGraph(CloseoutAgentState)
        builder.add_node("observe_project", self._closeout_observe_project)
        builder.add_node("synthesize_closeout", self._closeout_synthesize)
        builder.add_node("persist_candidate", self._closeout_persist)
        builder.add_edge(START, "observe_project")
        builder.add_edge("observe_project", "synthesize_closeout")
        builder.add_edge("synthesize_closeout", "persist_candidate")
        builder.add_edge("persist_candidate", END)
        return builder.compile(checkpointer=checkpointer)

    def _knowledge_observe_project(self, state: KnowledgeAgentState) -> dict[str, Any]:
        return {"project_state": self._observe_project(state, sequence=1)}

    def _closeout_observe_project(self, state: CloseoutAgentState) -> dict[str, Any]:
        return {"project_state": self._observe_project(state, sequence=1)}

    def _observe_project(
        self,
        state: KnowledgeAgentState | CloseoutAgentState,
        *,
        sequence: int,
    ) -> dict[str, Any] | None:
        run_id = state["run_id"]
        workspace_path = state.get("workspace_path")
        step_id = self.host.repository.start_agent_step(
            run_id=run_id,
            sequence=sequence,
            phase="observe",
            action_name="inspect_project_state",
            input_data={"workspace_path": workspace_path},
        )
        if not workspace_path:
            self.host.repository.finish_agent_step(
                step_id,
                status="skipped",
                output={"reason": "No workspace path was provided."},
            )
            return None
        tool_result = cast(
            dict[str, Any],
            self.host.tools.inspect_project_state.invoke({"workspace_path": workspace_path}),
        )
        if tool_result["status"] == "success":
            self.host.repository.finish_agent_step(step_id, status="completed", output=tool_result)
        else:
            self.host.repository.finish_agent_step(
                step_id,
                status="warning",
                output=tool_result,
                error={
                    "code": "project_state_unavailable",
                    "message": tool_result["summary"],
                    "safe_retry": tool_result["next_actions"][0],
                },
            )
        return cast(dict[str, Any], tool_result["state"])

    def _knowledge_initial_retrieval(self, state: KnowledgeAgentState) -> dict[str, Any]:
        step_id = self.host.repository.start_agent_step(
            run_id=state["run_id"],
            sequence=2,
            phase="act",
            action_name="search_knowledge",
            input_data={"query": state["task"]},
        )
        try:
            tool_result = cast(
                dict[str, Any],
                self.host.tools.search_knowledge.invoke(
                    {
                        "query": state["task"],
                        "workspace_path": state.get("workspace_path"),
                        "domain": state.get("selected_domain"),
                        "limit": 8,
                        "include_restricted": state.get("include_restricted", False),
                    }
                ),
            )
        except Exception as exc:
            self._record_step_failure(step_id, exc)
            raise
        items = cast(list[dict[str, Any]], tool_result["items"])
        self.host.repository.finish_agent_step(
            step_id,
            status="completed",
            output={
                "status": tool_result["status"],
                "summary": tool_result["summary"],
                "artifacts": tool_result["artifacts"],
                "result_count": len(items),
            },
        )
        return {"initial_results": items}

    def _knowledge_plan_retrieval(self, state: KnowledgeAgentState) -> dict[str, Any]:
        initial_results = state.get("initial_results", [])
        step_id = self.host.repository.start_agent_step(
            run_id=state["run_id"],
            sequence=3,
            phase="plan",
            action_name="deepseek_plan_retrieval",
            input_data={"initial_result_count": len(initial_results)},
        )
        try:
            planner = self.host.gateway.complete_json(
                task_type="agent_retrieval_plan",
                system_prompt=self.planner_prompt,
                payload={
                    "task": state["task"],
                    "initial_evidence": self.host._compact_results(initial_results, 6),
                    "project_state": state.get("project_state"),
                },
                agent_run_id=state["run_id"],
                complexity="simple",
                prompt_version=PLANNER.version,
                max_tokens=PLANNER.max_tokens,
                min_tokens=PLANNER.min_tokens,
                validator=lambda content: self.host._validate_plan(
                    content,
                    fallback_query=state["task"],
                ),
                validation_hint=(
                    "search_queries、catalog_queries、plan "
                    "必须是受限长度的字符串数组。"
                ),
            )
            content = self.host._validate_plan(planner.content, fallback_query=state["task"])
        except Exception as exc:
            self._record_step_failure(step_id, exc)
            raise
        self.host.repository.finish_agent_step(
            step_id,
            status="completed",
            output={**content, "model": planner.model, "usage": planner.usage},
        )
        return {"planner": content, "plan": content["plan"]}

    def _knowledge_execute_retrieval(self, state: KnowledgeAgentState) -> dict[str, Any]:
        planner = state["planner"]
        step_id = self.host.repository.start_agent_step(
            run_id=state["run_id"],
            sequence=4,
            phase="act",
            action_name="execute_langchain_tools",
            input_data=planner,
        )
        try:
            evidence = self._execute_retrieval_tools(state, planner)
        except Exception as exc:
            self._record_step_failure(step_id, exc)
            raise
        self.host.repository.finish_agent_step(
            step_id,
            status="completed",
            output={
                "knowledge_count": len(evidence["knowledge"]),
                "catalog_count": len(evidence["catalog"]),
                "tools": ["search_knowledge", "search_source_catalog"],
            },
        )
        return {"evidence": evidence}

    def _execute_retrieval_tools(
        self,
        state: KnowledgeAgentState,
        planner: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        knowledge_by_key: dict[str, dict[str, Any]] = {}
        for item in state.get("initial_results", []):
            knowledge_by_key[item["document_id"]] = item
        for query in planner["search_queries"]:
            tool_result = cast(
                dict[str, Any],
                self.host.tools.search_knowledge.invoke(
                    {
                        "query": query,
                        "workspace_path": state.get("workspace_path"),
                        "domain": state.get("selected_domain"),
                        "limit": 8,
                        "include_restricted": state.get("include_restricted", False),
                    }
                ),
            )
            for item in tool_result["items"]:
                knowledge_by_key[item["document_id"]] = item

        catalog_by_path: dict[str, dict[str, Any]] = {}
        for query in planner["catalog_queries"]:
            tool_result = cast(
                dict[str, Any],
                self.host.tools.search_source_catalog.invoke(
                    {
                        "query": query,
                        "limit": 8,
                        "workspace_path": state.get("workspace_path"),
                    }
                ),
            )
            for item in tool_result["items"]:
                catalog_by_path[item["source_uri"]] = item
        return {
            "knowledge": list(knowledge_by_key.values())[:24],
            "catalog": list(catalog_by_path.values())[:16],
        }

    def _knowledge_synthesize_evidence(self, state: KnowledgeAgentState) -> dict[str, Any]:
        evidence = state["evidence"]
        step_id = self.host.repository.start_agent_step(
            run_id=state["run_id"],
            sequence=5,
            phase="verify",
            action_name="deepseek_synthesize_evidence",
            input_data={
                "knowledge_count": len(evidence["knowledge"]),
                "catalog_count": len(evidence["catalog"]),
            },
        )
        try:
            synthesis = self.host.gateway.complete_json(
                task_type="agent_evidence_synthesis",
                system_prompt=self.synthesis_prompt,
                payload={
                    "task": state["task"],
                    "project_state": state.get("project_state"),
                    "knowledge_evidence": self.host._compact_results(evidence["knowledge"], 12),
                    "catalog_evidence": evidence["catalog"][:10],
                },
                agent_run_id=state["run_id"],
                complexity=state.get("complexity", "simple"),
                prompt_version=SYNTHESIS.version,
                max_tokens=SYNTHESIS.max_tokens,
                min_tokens=SYNTHESIS.min_tokens,
                validator=self.host._validate_synthesis,
                validation_hint="summary 和 current_state 不能为空，列表字段必须为数组。",
            )
            result = self.host._validate_synthesis(synthesis.content)
            result = self.host._guard_synthesis_evidence(result, evidence["knowledge"])
            result.update(
                {
                    "model": synthesis.model,
                    "usage": synthesis.usage,
                    "evidence_count": len(evidence["knowledge"]),
                    "catalog_count": len(evidence["catalog"]),
                }
            )
        except Exception as exc:
            self._record_step_failure(step_id, exc)
            raise
        self.host.repository.finish_agent_step(step_id, status="completed", output=result)
        return {"result": result}

    def _knowledge_persist_candidate(self, state: KnowledgeAgentState) -> dict[str, Any]:
        step_id = self.host.repository.start_agent_step(
            run_id=state["run_id"],
            sequence=6,
            phase="persist",
            action_name="persist_knowledge_candidate",
            input_data={"enabled": state.get("persist_result", False)},
        )
        result = dict(state["result"])
        if not state.get("persist_result", False):
            self.host.repository.finish_agent_step(
                step_id,
                status="skipped",
                output={"reason": "The caller did not request result persistence."},
            )
            return {"result": result}
        try:
            result["knowledge_item"] = self.host._persist_agent_result(
                task=state["task"],
                result=result,
                evidence=state["evidence"]["knowledge"],
                domain=state.get("selected_domain") or "work",
            )
        except Exception as exc:
            self._record_step_failure(step_id, exc)
            raise
        self.host.repository.finish_agent_step(
            step_id,
            status="completed",
            output={"knowledge_item": result["knowledge_item"]},
        )
        return {"result": result}

    def _compact_closeout_sources(self, sources: list[dict[str, Any]]) -> str:
        max_chars = self.host.settings.agent_max_context_chars
        per_source = max(800, max_chars // max(1, len(sources)))
        blocks: list[str] = []
        for source in sources:
            safe_text, _ = redact_secrets(source["text_content"])
            if len(safe_text) > per_source:
                head = max(240, per_source // 3)
                tail = safe_text[-(per_source - head) :]
                safe_text = f"{safe_text[:head]}\n\n[中间内容已压缩]\n\n{tail}"
            blocks.append(
                f"[source_id={source['source_id']}]\n"
                f"证据状态：{source.get('evidence_status', 'source_record')}\n"
                f"证据警告：{source.get('evidence_warning', '无')}\n"
                f"标题：{source['title']}\n{safe_text}"
            )
        return "\n\n---\n\n".join(blocks)[:max_chars]

    def _closeout_synthesize(self, state: CloseoutAgentState) -> dict[str, Any]:
        source_ids = state.get("source_ids") or [state["source_id"]]
        step_id = self.host.repository.start_agent_step(
            run_id=state["run_id"],
            sequence=2,
            phase="synthesize",
            action_name="deepseek_closeout",
            input_data={"source_ids": source_ids, "source_count": len(source_ids)},
        )
        try:
            sources: list[dict[str, Any]] = []
            for source_id in source_ids:
                source_result = cast(
                    dict[str, Any],
                    self.host.tools.read_source_document.invoke({"source_id": source_id}),
                )
                source = source_result.get("item")
                if source is None:
                    raise AgentValidationError(source_result["summary"])
                sources.append(source)
            safe_text = self._compact_closeout_sources(sources)
            response = self.host.gateway.complete_json(
                task_type=(
                    "codex_turn_closeout" if len(sources) == 1 else "codex_daily_closeout"
                ),
                system_prompt=self.closeout_prompt,
                payload={
                    "codex_user_tasks": safe_text,
                    "source_count": len(sources),
                    "project_state": state.get("project_state"),
                },
                agent_run_id=state["run_id"],
                complexity="simple",
                prompt_version=CLOSEOUT.version,
                max_tokens=CLOSEOUT.max_tokens,
                min_tokens=CLOSEOUT.min_tokens,
                validator=self.host._validate_closeout,
                validation_hint="title 和 summary 不能为空，状态与经验字段必须为数组。",
            )
            closeout = self.host._validate_closeout(response.content)
            closeout = self.host._guard_closeout_evidence(closeout, sources)
        except Exception as exc:
            self._record_step_failure(step_id, exc)
            raise
        self.host.repository.finish_agent_step(
            step_id,
            status="completed",
            output={**closeout, "model": response.model, "usage": response.usage},
        )
        return {
            "closeout": closeout,
            "model": response.model,
            "usage": response.usage,
            "source_privacy": state["source_privacy"],
        }

    def _closeout_persist(self, state: CloseoutAgentState) -> dict[str, Any]:
        source_ids = state.get("source_ids") or [state["source_id"]]
        step_id = self.host.repository.start_agent_step(
            run_id=state["run_id"],
            sequence=3,
            phase="persist",
            action_name="persist_closeout_candidate",
            input_data={"source_ids": source_ids},
        )
        try:
            closeout = state["closeout"]
            if closeout.get("evidence_status") == "user_request_only":
                result = {
                    **closeout,
                    "knowledge_item": None,
                    "persistence_status": "skipped_without_independent_evidence",
                    "model": state["model"],
                    "usage": state["usage"],
                    "project_state_observed": state.get("project_state") is not None,
                }
                self.host.repository.finish_agent_step(
                    step_id,
                    status="skipped",
                    output=result,
                )
                return {"result": result}
            knowledge = self.host.repository.create_knowledge_candidate(
                domain="work",
                knowledge_type="project_state",
                title=closeout["title"],
                content=self.host._closeout_content(closeout, state.get("project_state")),
                confidence=closeout["confidence"],
                privacy=state["source_privacy"],
                evidence_ids=source_ids,
            )
            result = {
                **closeout,
                "knowledge_item": knowledge,
                "model": state["model"],
                "usage": state["usage"],
                "project_state_observed": state.get("project_state") is not None,
            }
        except Exception as exc:
            self._record_step_failure(step_id, exc)
            raise
        self.host.repository.finish_agent_step(step_id, status="completed", output=result)
        return {"result": result}

    def _record_step_failure(self, step_id: str, exc: Exception) -> None:
        self.host.repository.finish_agent_step(
            step_id,
            status="warning" if isinstance(exc, LLMError) else "failed",
            error=self.host._error_payload(exc),
        )

    def _config(self, run_id: str, graph_kind: GraphKind) -> RunnableConfig:
        return {
            "configurable": {"thread_id": self._thread_id(run_id, graph_kind)},
            "recursion_limit": self.host.settings.agent_max_tool_rounds + 8,
        }

    @staticmethod
    def _thread_id(run_id: str, graph_kind: GraphKind) -> str:
        return f"pkas:{graph_kind}:{run_id}"
