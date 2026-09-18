import json
from dataclasses import asdict
from typing import Any

from pkas.agent_errors import AgentValidationError
from pkas.agent_graph import AgentGraphRuntime, CloseoutAgentState, GraphKind, KnowledgeAgentState
from pkas.agent_tools import AgentToolset
from pkas.codex_capture import redact_secrets
from pkas.config import Settings, get_settings
from pkas.llm import DeepSeekGateway, LLMError
from pkas.project_state import ProjectInspectionError, ProjectInspector
from pkas.prompts import CLOSEOUT, PLANNER, SYNTHESIS
from pkas.repository import Repository
from pkas.retrieval import RetrievalService

WORK_TERMS = {
    "业务",
    "项目",
    "代码",
    "开发",
    "服务器",
    "部署",
    "设备",
    "数据库",
    "接口",
    "客户",
    "合同",
    "故障",
    "测试",
    "报告",
}
SELF_TERMS = {
    "我",
    "自己",
    "性格",
    "偏好",
    "聊天",
    "习惯",
    "人生",
    "情绪",
    "价值观",
    "表达",
    "学习状态",
    "蒸馏",
}

PLANNER_PROMPT = PLANNER.system_prompt
SYNTHESIS_PROMPT = SYNTHESIS.system_prompt
CLOSEOUT_PROMPT = CLOSEOUT.system_prompt


class AgentService:
    def __init__(
        self,
        repository: Repository | None = None,
        *,
        settings: Settings | None = None,
        gateway: DeepSeekGateway | None = None,
        inspector: ProjectInspector | None = None,
        retrieval: RetrievalService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or Repository()
        self.gateway = gateway or DeepSeekGateway(
            settings=self.settings,
            repository=self.repository,
        )
        self.inspector = inspector or ProjectInspector(self.repository)
        self.retrieval = retrieval
        self.tools = AgentToolset(self.repository, self.inspector, self.retrieval)
        self.graphs = AgentGraphRuntime(
            self,
            planner_prompt=PLANNER_PROMPT,
            synthesis_prompt=SYNTHESIS_PROMPT,
            closeout_prompt=CLOSEOUT_PROMPT,
        )

    def _search_knowledge(
        self,
        query: str,
        *,
        domain: str | None,
        workspace_path: str | None = None,
        limit: int,
        include_restricted: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.retrieval is None:
            return (
                self.repository.search(
                    query,
                    workspace_path=workspace_path,
                    domain=domain,
                    limit=limit,
                    include_restricted=include_restricted,
                ),
                {
                    "mode": "fts_only_compatibility",
                    "warnings": ["Unified retrieval service is unavailable."],
                    "health": None,
                },
            )
        response = self.retrieval.search(
            query,
            workspace_path=workspace_path,
            domain=domain,
            limit=limit,
            include_restricted=include_restricted,
        )
        return response.results, {
            "mode": response.mode,
            "warnings": response.warnings,
            "health": asdict(response.health) if response.health else None,
        }

    @staticmethod
    def select_domain(task: str, requested_domain: str | None = None) -> str | None:
        if requested_domain:
            return requested_domain
        work_score = sum(1 for term in WORK_TERMS if term in task)
        self_score = sum(1 for term in SELF_TERMS if term in task)
        if work_score > self_score:
            return "work"
        if self_score > work_score:
            return "self"
        return None

    def prepare_context(
        self,
        *,
        task: str,
        domain: str | None,
        workspace_path: str | None = None,
        limit: int,
        include_restricted: bool,
    ) -> dict[str, Any]:
        selected_domain = self.select_domain(task, domain)
        plan = [
            "识别任务领域与隐私范围",
            "执行本地严格检索和宽召回",
            "返回可定位的上下文片段",
            "由调用方完成推理、执行和结果验证",
        ]
        results, retrieval_meta = self._search_knowledge(
            task,
            domain=selected_domain,
            workspace_path=workspace_path,
            limit=limit,
            include_restricted=include_restricted,
        )
        context = {
            "selected_domain": selected_domain or "all",
            "include_restricted": include_restricted,
            "results": results,
            "retrieval": retrieval_meta,
        }
        summary = (
            f"已为任务准备 {len(results)} 条上下文，检索范围为 {selected_domain or '全部领域'}"
        )
        run_id = self.repository.record_agent_run(
            task=task,
            selected_domain=selected_domain,
            plan=plan,
            context=context,
            result={"summary": summary, "mode": "local_context"},
        )
        return {
            "run_id": run_id,
            "task": task,
            "selected_domain": selected_domain,
            "plan": plan,
            "context": results,
            "summary": summary,
            "mode": "local_context",
        }

    def run(
        self,
        *,
        task: str,
        workspace_path: str | None = None,
        domain: str | None = None,
        include_restricted: bool = False,
        persist_result: bool = False,
        complexity: str = "simple",
    ) -> dict[str, Any]:
        safe_task, _ = redact_secrets(task[:12_000])
        selected_domain = self.select_domain(safe_task, domain)
        run_id = self.repository.create_agent_run(
            task=safe_task,
            selected_domain=selected_domain,
            context={
                "framework": "langgraph",
                "graph_kind": "knowledge",
                "workspace_path": workspace_path,
            },
        )
        state: KnowledgeAgentState = {
            "run_id": run_id,
            "graph_kind": "knowledge",
            "task": safe_task,
            "workspace_path": workspace_path,
            "selected_domain": selected_domain,
            "include_restricted": include_restricted,
            "persist_result": persist_result,
            "complexity": complexity,
            "project_state": None,
            "initial_results": [],
            "planner": {},
            "plan": [],
            "evidence": {"knowledge": [], "catalog": []},
            "result": {},
        }
        try:
            completed = self.graphs.invoke_knowledge(state)
        except Exception as exc:
            return self._finish_graph_failure(
                run_id=run_id,
                graph_kind="knowledge",
                selected_domain=selected_domain,
                exc=exc,
            )
        return self._finish_knowledge_graph(run_id, completed)

    def closeout_codex_turn(
        self,
        *,
        source_id: str,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        return self.closeout_codex_batch(
            source_ids=[source_id],
            workspace_path=workspace_path,
        )

    def closeout_codex_batch(
        self,
        *,
        source_ids: list[str],
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        unique_source_ids = list(dict.fromkeys(source_ids))
        sources = [
            source
            for source_id in unique_source_ids
            if (source := self.repository.source_document(source_id)) is not None
        ]
        if not sources or len(sources) != len(unique_source_ids):
            raise AgentValidationError("Codex 任务来源不存在或批次不完整。")
        latest_source = sources[-1]
        resolved_workspace = workspace_path or latest_source["metadata"].get("cwd")
        task = (
            f"整理 Codex 任务完成后的真实开发状态：{latest_source['title']}"
            if len(sources) == 1
            else f"聚合整理当天 Codex 任务的真实开发状态：{latest_source['title']}"
        )
        privacy = "public"
        if any(source["privacy"] == "restricted" for source in sources):
            privacy = "restricted"
        elif any(source["privacy"] == "private" for source in sources):
            privacy = "private"
        run_id = self.repository.create_agent_run(
            task=task,
            selected_domain="work",
            context={
                "framework": "langgraph",
                "graph_kind": "closeout",
                "source_id": unique_source_ids[-1],
                "source_ids": unique_source_ids,
                "workspace_path": resolved_workspace,
            },
        )
        state: CloseoutAgentState = {
            "run_id": run_id,
            "graph_kind": "closeout",
            "source_id": unique_source_ids[-1],
            "source_ids": unique_source_ids,
            "workspace_path": resolved_workspace,
            "source_title": latest_source["title"],
            "project_state": None,
            "closeout": {},
            "model": "",
            "usage": {},
            "source_privacy": privacy,
            "result": {},
        }
        try:
            completed = self.graphs.invoke_closeout(state)
        except Exception as exc:
            return self._finish_graph_failure(
                run_id=run_id,
                graph_kind="closeout",
                selected_domain="work",
                exc=exc,
            )
        return self._finish_closeout_graph(run_id, completed)

    def resume(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_agent_run(run_id)
        if run is None:
            return {
                "status": "failed",
                "run_id": run_id,
                "error": {
                    "code": "agent_run_not_found",
                    "message": "Agent run does not exist.",
                    "retryable": False,
                    "safe_retry": "Use an existing Agent run identifier.",
                    "stop_condition": "Stop when the run identifier cannot be resolved.",
                },
            }
        if run["status"] == "completed":
            return {
                "status": "completed",
                "run_id": run_id,
                "framework": "langgraph",
                "result": run["result"],
                "already_completed": True,
            }
        graph_kind = run["context"].get("graph_kind")
        if graph_kind not in {"knowledge", "closeout"}:
            return {
                "status": "failed",
                "run_id": run_id,
                "error": {
                    "code": "agent_run_not_resumable",
                    "message": "This run was not created by the LangGraph runtime.",
                    "retryable": False,
                    "safe_retry": "Start a new LangGraph Agent run.",
                    "stop_condition": "Stop when no LangGraph checkpoint exists.",
                },
            }
        checkpoint = self.graphs.status(run_id, graph_kind)
        if not checkpoint["resumable"]:
            return {
                "status": "failed",
                "run_id": run_id,
                "framework": "langgraph",
                "checkpoint": self._public_checkpoint(checkpoint),
                "error": {
                    "code": "agent_checkpoint_not_resumable",
                    "message": "The graph has no pending node to resume.",
                    "retryable": False,
                    "safe_retry": "Start a new Agent run if the previous state is incomplete.",
                    "stop_condition": "Stop when the checkpoint has no next node.",
                },
            }
        self.repository.reopen_agent_run(run_id)
        try:
            completed = self.graphs.resume(run_id, graph_kind)
        except Exception as exc:
            return self._finish_graph_failure(
                run_id=run_id,
                graph_kind=graph_kind,
                selected_domain=run["selected_domain"],
                exc=exc,
            )
        if graph_kind == "knowledge":
            return self._finish_knowledge_graph(run_id, completed)
        return self._finish_closeout_graph(run_id, completed)

    def graph_status(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_agent_run(run_id)
        if run is None:
            raise AgentValidationError("Agent run does not exist.")
        graph_kind = run["context"].get("graph_kind")
        if graph_kind not in {"knowledge", "closeout"}:
            return {
                "run_id": run_id,
                "run_status": run["status"],
                "framework": "legacy",
                "resumable": False,
            }
        checkpoint = self.graphs.status(run_id, graph_kind)
        return {
            "run_id": run_id,
            "run_status": run["status"],
            **self._public_checkpoint(checkpoint),
        }

    def _finish_knowledge_graph(
        self,
        run_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        result = state.get("result")
        if not isinstance(result, dict):
            raise AgentValidationError("LangGraph knowledge run completed without a result.")
        checkpoint = self.graphs.status(run_id, "knowledge")
        context = self._graph_context(state, checkpoint)
        plan = state.get("plan", [])
        self.repository.finish_agent_run(
            run_id,
            status="completed",
            plan=plan,
            context=context,
            result=result,
        )
        return {
            "status": "completed",
            "run_id": run_id,
            "framework": "langgraph",
            "plan": plan,
            "context": {
                "project_state": state.get("project_state"),
                "evidence": state.get("evidence", {}),
            },
            "checkpoint": self._public_checkpoint(checkpoint),
            "result": result,
        }

    def _finish_closeout_graph(
        self,
        run_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        result = state.get("result")
        if not isinstance(result, dict):
            raise AgentValidationError("LangGraph closeout run completed without a result.")
        checkpoint = self.graphs.status(run_id, "closeout")
        context = self._graph_context(state, checkpoint)
        plan = ["读取任务证据", "检查授权工作区", "提炼开发状态", "写入知识候选"]
        self.repository.finish_agent_run(
            run_id,
            status="completed",
            plan=plan,
            context=context,
            result=result,
        )
        return {
            "status": "completed",
            "run_id": run_id,
            "framework": "langgraph",
            "checkpoint": self._public_checkpoint(checkpoint),
            "result": result,
        }

    def _finish_graph_failure(
        self,
        *,
        run_id: str,
        graph_kind: GraphKind,
        selected_domain: str | None,
        exc: Exception,
    ) -> dict[str, Any]:
        checkpoint: dict[str, Any]
        try:
            checkpoint = self.graphs.status(run_id, graph_kind)
        except Exception:
            checkpoint = {
                "framework": "langgraph",
                "graph_kind": graph_kind,
                "checkpoint_id": None,
                "next_nodes": [],
                "resumable": False,
                "history_count": 0,
                "state_keys": [],
                "values": {},
            }
        state = checkpoint.get("values", {})
        error = self._error_payload(exc)
        run_status = "warning" if isinstance(exc, LLMError) else "failed"
        self.repository.finish_agent_run(
            run_id,
            status=run_status,
            plan=state.get("plan", []),
            context=self._graph_context(state, checkpoint),
            result={"error": error},
        )
        return {
            "status": run_status,
            "run_id": run_id,
            "framework": "langgraph",
            "selected_domain": selected_domain,
            "plan": state.get("plan", []),
            "context": {
                "project_state": state.get("project_state"),
                "evidence": state.get("evidence", {}),
            },
            "checkpoint": self._public_checkpoint(checkpoint),
            "error": error,
        }

    @staticmethod
    def _graph_context(state: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
        return {
            "framework": "langgraph",
            "graph_kind": checkpoint.get("graph_kind") or state.get("graph_kind"),
            "workspace_path": state.get("workspace_path"),
            "source_id": state.get("source_id"),
            "source_ids": state.get("source_ids", []),
            "project_state": state.get("project_state"),
            "evidence": state.get("evidence", {}),
            "checkpoint": AgentService._public_checkpoint(checkpoint),
        }

    @staticmethod
    def _public_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
        return {
            "framework": checkpoint.get("framework", "langgraph"),
            "graph_kind": checkpoint.get("graph_kind"),
            "thread_id": checkpoint.get("thread_id"),
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "next_nodes": checkpoint.get("next_nodes", []),
            "resumable": checkpoint.get("resumable", False),
            "history_count": checkpoint.get("history_count", 0),
            "state_keys": checkpoint.get("state_keys", []),
        }

    def _run_without_langgraph(
        self,
        *,
        task: str,
        workspace_path: str | None = None,
        domain: str | None = None,
        include_restricted: bool = False,
        persist_result: bool = False,
        complexity: str = "simple",
    ) -> dict[str, Any]:
        safe_task, _ = redact_secrets(task[:12_000])
        selected_domain = self.select_domain(safe_task, domain)
        run_id = self.repository.create_agent_run(
            task=safe_task,
            selected_domain=selected_domain,
            context={"workspace_path": workspace_path},
        )
        plan: list[str] = []
        context: dict[str, Any] = {}
        active_step: str | None = None
        try:
            project_state = self._observe_project(run_id, workspace_path, sequence=1)
            context["project_state"] = project_state

            initial_results, initial_retrieval_meta = self._search_knowledge(
                safe_task,
                domain=selected_domain,
                workspace_path=workspace_path,
                limit=8,
                include_restricted=include_restricted,
            )
            context["initial_retrieval"] = initial_retrieval_meta
            active_step = self.repository.start_agent_step(
                run_id=run_id,
                sequence=2,
                phase="plan",
                action_name="deepseek_plan_retrieval",
                input_data={"initial_result_count": len(initial_results)},
            )
            planner = self.gateway.complete_json(
                task_type="agent_retrieval_plan",
                system_prompt=PLANNER_PROMPT,
                payload={
                    "task": safe_task,
                    "initial_evidence": self._compact_results(initial_results, 6),
                    "project_state": project_state,
                },
                agent_run_id=run_id,
                complexity="simple",
                prompt_version=PLANNER.version,
                max_tokens=PLANNER.max_tokens,
                min_tokens=PLANNER.min_tokens,
                validator=lambda content: self._validate_plan(
                    content,
                    fallback_query=task,
                ),
                validation_hint=(
                    "search_queries、catalog_queries、plan "
                    "必须是受限长度的字符串数组。"
                ),
            )
            planner_content = self._validate_plan(planner.content, fallback_query=safe_task)
            plan = planner_content["plan"]
            self.repository.finish_agent_step(
                active_step,
                status="completed",
                output={
                    **planner_content,
                    "model": planner.model,
                    "usage": planner.usage,
                },
            )
            active_step = None

            active_step = self.repository.start_agent_step(
                run_id=run_id,
                sequence=3,
                phase="act",
                action_name="execute_local_retrieval",
                input_data=planner_content,
            )
            evidence = self._execute_retrieval(
                planner_content,
                initial_results=initial_results,
                domain=selected_domain,
                workspace_path=workspace_path,
                include_restricted=include_restricted,
            )
            context["evidence"] = evidence
            self.repository.finish_agent_step(
                active_step,
                status="completed",
                output={
                    "knowledge_count": len(evidence["knowledge"]),
                    "catalog_count": len(evidence["catalog"]),
                },
            )
            active_step = None

            active_step = self.repository.start_agent_step(
                run_id=run_id,
                sequence=4,
                phase="verify",
                action_name="deepseek_synthesize_evidence",
                input_data={
                    "knowledge_count": len(evidence["knowledge"]),
                    "catalog_count": len(evidence["catalog"]),
                },
            )
            synthesis = self.gateway.complete_json(
                task_type="agent_evidence_synthesis",
                system_prompt=SYNTHESIS_PROMPT,
                payload={
                    "task": safe_task,
                    "project_state": project_state,
                    "knowledge_evidence": self._compact_results(evidence["knowledge"], 12),
                    "catalog_evidence": evidence["catalog"][:10],
                },
                agent_run_id=run_id,
                complexity=complexity,
                prompt_version=SYNTHESIS.version,
                max_tokens=SYNTHESIS.max_tokens,
                min_tokens=SYNTHESIS.min_tokens,
                validator=self._validate_synthesis,
                validation_hint="summary 和 current_state 不能为空，列表字段必须为数组。",
            )
            result = self._validate_synthesis(synthesis.content)
            result = self._guard_synthesis_evidence(result, evidence["knowledge"])
            result.update(
                {
                    "model": synthesis.model,
                    "usage": synthesis.usage,
                    "evidence_count": len(evidence["knowledge"]),
                    "catalog_count": len(evidence["catalog"]),
                }
            )
            self.repository.finish_agent_step(
                active_step,
                status="completed",
                output=result,
            )
            active_step = None

            if persist_result:
                result["knowledge_item"] = self._persist_agent_result(
                    task=safe_task,
                    result=result,
                    evidence=evidence["knowledge"],
                    domain=selected_domain or "work",
                )
            self.repository.finish_agent_run(
                run_id,
                status="completed",
                plan=plan,
                context=context,
                result=result,
            )
            return {
                "status": "completed",
                "run_id": run_id,
                "plan": plan,
                "context": context,
                "result": result,
            }
        except (LLMError, AgentValidationError, ProjectInspectionError) as exc:
            error = self._error_payload(exc)
            if active_step:
                self.repository.finish_agent_step(
                    active_step,
                    status="warning" if isinstance(exc, LLMError) else "failed",
                    error=error,
                )
            self.repository.finish_agent_run(
                run_id,
                status="warning" if isinstance(exc, LLMError) else "failed",
                plan=plan,
                context=context,
                result={"error": error},
            )
            return {
                "status": "warning" if isinstance(exc, LLMError) else "failed",
                "run_id": run_id,
                "plan": plan,
                "context": context,
                "error": error,
            }

    def _closeout_without_langgraph(
        self,
        *,
        source_id: str,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        source = self.repository.source_document(source_id)
        if not source:
            raise AgentValidationError("Codex 任务来源不存在。")
        metadata = source["metadata"]
        resolved_workspace = workspace_path or metadata.get("cwd")
        task = f"整理 Codex 任务完成后的真实开发状态：{source['title']}"
        run_id = self.repository.create_agent_run(
            task=task,
            selected_domain="work",
            context={"source_id": source_id, "workspace_path": resolved_workspace},
        )
        project_state: dict[str, Any] | None = None
        active_step: str | None = None
        try:
            project_state = self._observe_project(run_id, resolved_workspace, sequence=1)
            safe_text, _ = redact_secrets(
                source["text_content"][: self.settings.agent_max_context_chars]
            )
            active_step = self.repository.start_agent_step(
                run_id=run_id,
                sequence=2,
                phase="synthesize",
                action_name="deepseek_closeout",
                input_data={"source_id": source_id},
            )
            response = self.gateway.complete_json(
                task_type="codex_turn_closeout",
                system_prompt=CLOSEOUT_PROMPT,
                payload={
                    "codex_turn": safe_text,
                    "project_state": project_state,
                },
                agent_run_id=run_id,
                complexity="simple",
                prompt_version=CLOSEOUT.version,
                max_tokens=CLOSEOUT.max_tokens,
                min_tokens=CLOSEOUT.min_tokens,
                validator=self._validate_closeout,
                validation_hint="title 和 summary 不能为空，状态与经验字段必须为数组。",
            )
            closeout = self._validate_closeout(response.content)
            closeout = self._guard_closeout_evidence(closeout, [source])
            knowledge = self.repository.create_knowledge_candidate(
                domain="work",
                knowledge_type="project_state",
                title=closeout["title"],
                content=self._closeout_content(closeout, project_state),
                confidence=closeout["confidence"],
                privacy=source["privacy"],
                evidence_ids=[source_id],
            )
            result = {
                **closeout,
                "knowledge_item": knowledge,
                "model": response.model,
                "usage": response.usage,
                "project_state_observed": project_state is not None,
            }
            self.repository.finish_agent_step(active_step, status="completed", output=result)
            active_step = None
            self.repository.finish_agent_run(
                run_id,
                status="completed",
                plan=["读取任务证据", "检查授权工作区", "提炼开发状态", "写入知识候选"],
                context={"source_id": source_id, "project_state": project_state},
                result=result,
            )
            return {"status": "completed", "run_id": run_id, "result": result}
        except (LLMError, AgentValidationError, ProjectInspectionError) as exc:
            error = self._error_payload(exc)
            if active_step:
                self.repository.finish_agent_step(
                    active_step,
                    status="warning" if isinstance(exc, LLMError) else "failed",
                    error=error,
                )
            self.repository.finish_agent_run(
                run_id,
                status="warning" if isinstance(exc, LLMError) else "failed",
                plan=["读取任务证据", "检查授权工作区", "提炼开发状态", "写入知识候选"],
                context={"source_id": source_id, "project_state": project_state},
                result={"error": error},
            )
            return {
                "status": "warning" if isinstance(exc, LLMError) else "failed",
                "run_id": run_id,
                "error": error,
            }

    def _observe_project(
        self,
        run_id: str,
        workspace_path: str | None,
        *,
        sequence: int,
    ) -> dict[str, Any] | None:
        step = self.repository.start_agent_step(
            run_id=run_id,
            sequence=sequence,
            phase="observe",
            action_name="inspect_project_state",
            input_data={"workspace_path": workspace_path},
        )
        if not workspace_path:
            self.repository.finish_agent_step(
                step,
                status="skipped",
                output={"reason": "任务没有提供工作区"},
            )
            return None
        try:
            result = self.inspector.inspect(workspace_path)
        except ProjectInspectionError as exc:
            self.repository.finish_agent_step(
                step,
                status="warning",
                error=self._error_payload(exc),
            )
            return {"status": "warning", "summary": str(exc)}
        self.repository.finish_agent_step(step, status="completed", output=result)
        return result

    def _execute_retrieval(
        self,
        plan: dict[str, Any],
        *,
        initial_results: list[dict[str, Any]],
        domain: str | None,
        workspace_path: str | None,
        include_restricted: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        knowledge_by_key: dict[str, dict[str, Any]] = {}
        for item in initial_results:
            knowledge_by_key[item["document_id"]] = item
        for query in plan["search_queries"]:
            items, _retrieval_meta = self._search_knowledge(
                query,
                domain=domain,
                workspace_path=workspace_path,
                limit=8,
                include_restricted=include_restricted,
            )
            for item in items:
                knowledge_by_key[item["document_id"]] = item
        catalog_by_path: dict[str, dict[str, Any]] = {}
        for query in plan["catalog_queries"]:
            for item in self.repository.search_source_catalog(query, limit=8):
                catalog_by_path[item["source_uri"]] = item
        return {
            "knowledge": list(knowledge_by_key.values())[:24],
            "catalog": list(catalog_by_path.values())[:16],
        }

    def _persist_agent_result(
        self,
        *,
        task: str,
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
        domain: str,
    ) -> dict[str, Any]:
        selected_ids = set(result.get("evidence_ids") or [])
        evidence_ids = [
            item["source_id"]
            for item in evidence
            if item.get("source_id")
            and (
                not selected_ids
                or item.get("source_id") in selected_ids
                or item.get("document_id") in selected_ids
            )
        ]
        content = "\n".join(
            [
                result["summary"],
                "",
                f"当前状态：{result['current_state']}",
                "",
                "下一步：",
                *[f"- {item}" for item in result["next_actions"]],
            ]
        )
        return self.repository.create_knowledge_candidate(
            domain=domain,
            knowledge_type="agent_result",
            title=f"Agent 任务：{task[:120]}",
            content=content,
            confidence=result["confidence"],
            privacy="private",
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def _validate_plan(content: dict[str, Any], *, fallback_query: str) -> dict[str, Any]:
        search_queries = AgentService._string_list(content.get("search_queries"), limit=3)
        catalog_queries = AgentService._string_list(content.get("catalog_queries"), limit=2)
        plan = AgentService._string_list(content.get("plan"), limit=5)
        return {
            "search_queries": search_queries or [fallback_query[:200]],
            "catalog_queries": catalog_queries,
            "plan": plan or ["检索知识证据", "综合当前状态", "返回下一步"],
        }

    @staticmethod
    def _validate_synthesis(content: dict[str, Any]) -> dict[str, Any]:
        summary = str(content.get("summary") or "").strip()
        current_state = str(content.get("current_state") or "").strip()
        if not summary or not current_state:
            raise AgentValidationError("Agent 综合结果缺少 summary 或 current_state。")
        confidence = str(content.get("confidence") or "low")
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
        return {
            "summary": summary[:4000],
            "current_state": current_state[:6000],
            "completed": AgentService._string_list(content.get("completed"), limit=20),
            "remaining": AgentService._string_list(content.get("remaining"), limit=20),
            "risks": AgentService._string_list(content.get("risks"), limit=20),
            "next_actions": AgentService._string_list(content.get("next_actions"), limit=20),
            "evidence_ids": AgentService._string_list(content.get("evidence_ids"), limit=40),
            "confidence": confidence,
        }

    @staticmethod
    def _validate_closeout(content: dict[str, Any]) -> dict[str, Any]:
        title = str(content.get("title") or "").strip()
        summary = str(content.get("summary") or "").strip()
        if not title or not summary:
            raise AgentValidationError("Agent 收尾结果缺少 title 或 summary。")
        confidence = str(content.get("confidence") or "low")
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
        return {
            "title": title[:300],
            "summary": summary[:5000],
            "completed": AgentService._string_list(content.get("completed"), limit=30),
            "remaining": AgentService._string_list(content.get("remaining"), limit=30),
            "risks": AgentService._string_list(content.get("risks"), limit=20),
            "reusable_lessons": AgentService._string_list(
                content.get("reusable_lessons"),
                limit=20,
            ),
            "evidence_ids": AgentService._string_list(content.get("evidence_ids"), limit=40),
            "confidence": confidence,
        }

    @staticmethod
    def _guard_closeout_evidence(
        closeout: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        task_requests_only = bool(sources) and all(
            source.get("source_type") == "codex-turn"
            and source.get("evidence_status") == "user_request_only"
            for source in sources
        )
        if not task_requests_only:
            return closeout

        claimed_completed = closeout.get("completed") or []
        remaining = list(closeout.get("remaining") or [])
        remaining.extend(
            f"缺少独立机器证据，不能确认：{item}" for item in claimed_completed
        )
        warning = (
            "输入来源只有用户当时提出的任务，没有命令输出、测试日志、Git 归因或外部回执；"
            "本候选不能证明任务已经完成。"
        )
        risks = [warning, *(closeout.get("risks") or [])]
        return {
            **closeout,
            "title": f"待核验：{sources[-1].get('title') or closeout['title']}",
            "summary": warning,
            "completed": [],
            "remaining": remaining,
            "risks": list(dict.fromkeys(risks)),
            "reusable_lessons": [],
            "confidence": "low",
            "evidence_status": "user_request_only",
        }

    @staticmethod
    def _guard_synthesis_evidence(
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        selected_ids = set(result.get("evidence_ids") or [])
        supporting = [
            item
            for item in evidence
            if not selected_ids
            or item.get("source_id") in selected_ids
            or item.get("document_id") in selected_ids
        ]
        task_requests_only = bool(supporting) and all(
            item.get("evidence_status") == "user_request_only"
            for item in supporting
        )
        if not task_requests_only:
            return result

        claimed_completed = result.get("completed") or []
        remaining = list(result.get("remaining") or [])
        remaining.extend(
            f"缺少独立机器证据，不能确认：{item}" for item in claimed_completed
        )
        warning = (
            "检索依据只有用户当时提出的任务，没有机器可验证的执行证据；"
            "不能据此确认修改、测试、部署或验收已经完成。"
        )
        return {
            **result,
            "summary": warning,
            "current_state": warning,
            "completed": [],
            "remaining": remaining,
            "risks": list(dict.fromkeys([warning, *(result.get("risks") or [])])),
            "confidence": "low",
            "evidence_status": "user_request_only",
        }

    @staticmethod
    def _string_list(value: Any, *, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:1000] for item in value if str(item).strip()][:limit]

    def _compact_results(self, results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        remaining = self.settings.agent_max_context_chars
        compact: list[dict[str, Any]] = []
        for item in results[:limit]:
            safe_snippet, _ = redact_secrets(str(item.get("snippet") or "")[:1200])
            safe_title, _ = redact_secrets(str(item.get("title") or "")[:300])
            safe_uri, _ = redact_secrets(str(item.get("original_uri") or "")[:2000])
            record = {
                "source_id": item.get("source_id"),
                "document_id": item.get("document_id"),
                "title": safe_title,
                "locator": item.get("locator"),
                "original_uri": safe_uri,
                "snippet": safe_snippet,
                "evidence_status": item.get("evidence_status"),
                "evidence_warning": item.get("evidence_warning"),
            }
            encoded = json.dumps(record, ensure_ascii=False)
            if len(encoded) > remaining:
                break
            compact.append(record)
            remaining -= len(encoded)
        return compact

    @staticmethod
    def _closeout_content(
        closeout: dict[str, Any],
        project_state: dict[str, Any] | None,
    ) -> str:
        sections = [closeout["summary"]]
        for title, key in (
            ("已完成", "completed"),
            ("遗留事项", "remaining"),
            ("风险", "risks"),
            ("可复用经验", "reusable_lessons"),
        ):
            values = closeout[key]
            if values:
                sections.extend(["", f"## {title}", *[f"- {item}" for item in values]])
        if project_state:
            sections.extend(
                [
                    "",
                    "## 只读现场证据",
                    f"- 状态：{project_state.get('summary', '未取得')}",
                    f"- 观测时间：{project_state.get('observed_at', '未知')}",
                ]
            )
        return "\n".join(sections)

    @staticmethod
    def _error_payload(exc: Exception) -> dict[str, Any]:
        code = exc.code if isinstance(exc, LLMError) else type(exc).__name__
        retryable = exc.retryable if isinstance(exc, LLMError) else False
        return {
            "code": code,
            "message": str(exc),
            "retryable": retryable,
            "safe_retry": (
                "检查 DeepSeek 配置或等待临时网络故障恢复后重试。"
                if retryable
                else "修正输入或授权范围后再运行，不要盲目重试。"
            ),
            "stop_condition": "达到 Token 预算、鉴权失败或工作区未授权时停止。",
        }
