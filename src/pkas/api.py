from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pkas.config import Settings, get_settings
from pkas.ingest import ImportBoundaryError
from pkas.schemas import (
    AgentContextRequest,
    ChatLabImportRequest,
    ChatLabInspectRequest,
    CustomerReplyContextRequest,
    CustomerSearchRequest,
    CustomerSignalRequest,
    CustomerUpdateRequest,
    DistillationCandidateRequest,
    DistillationExportRequest,
    Envelope,
    ImportInspectRequest,
    ImportRunRequest,
    PersonaCandidateRequest,
    ReviewRequest,
    SearchRequest,
    WeFlowExportDiscoverRequest,
    WeFlowExportImportRequest,
    WeFlowExportInspectRequest,
)
from pkas.system import KnowledgeSystem


def success(
    summary: str,
    data: Any = None,
    *,
    next_actions: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> Envelope:
    return Envelope(
        status="success",
        summary=summary,
        data=data,
        next_actions=next_actions or [],
        artifacts=artifacts or [],
    )


def warning(
    summary: str,
    data: Any = None,
    *,
    next_actions: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> Envelope:
    return Envelope(
        status="warning",
        summary=summary,
        data=data,
        next_actions=next_actions or [],
        artifacts=artifacts or [],
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        system = KnowledgeSystem.create(resolved_settings)
        system.database.initialize()
        app.state.system = system
        yield

    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    def system_from(request: Request) -> KnowledgeSystem:
        return request.app.state.system

    @app.get("/api/health", response_model=Envelope)
    def health(request: Request) -> Envelope:
        system = system_from(request)
        details = system.database.health()
        details["app_version"] = system.settings.app_version
        return success("个人知识系统运行正常", details)

    @app.get("/api/dashboard", response_model=Envelope)
    def dashboard(request: Request) -> Envelope:
        stats = system_from(request).repository.stats()
        return success("已读取个人知识系统概览", stats)

    @app.post("/api/search", response_model=Envelope)
    def search(payload: SearchRequest, request: Request) -> Envelope:
        results = system_from(request).repository.search(
            payload.query,
            domain=payload.domain,
            limit=payload.limit,
            include_restricted=payload.include_restricted,
        )
        return success(f"找到 {len(results)} 条带来源的知识片段", results)

    @app.get("/api/sources", response_model=Envelope)
    def sources(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Envelope:
        items = system_from(request).repository.list_sources(limit)
        return success(f"已读取 {len(items)} 个资料来源", items)

    @app.get("/api/documents/{document_id}", response_model=Envelope)
    def document(
        document_id: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=12000, ge=1, le=50000),
    ) -> Envelope:
        item = system_from(request).repository.read_document(document_id, offset, limit)
        if not item:
            raise HTTPException(status_code=404, detail="资料不存在")
        return success("已读取原文与来源定位", item, artifacts=[item["vault_path"]])

    @app.post("/api/import/inspect", response_model=Envelope)
    def inspect_import(payload: ImportInspectRequest, request: Request) -> Envelope:
        try:
            result = system_from(request).ingestion.inspect_path(
                payload.path,
                payload.recursive,
            )
        except (ImportBoundaryError, FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success(
            "导入范围检查完成，尚未复制或索引任何文件",
            result,
            next_actions=["确认范围、业务/自我领域和隐私级别后再执行导入"],
        )

    @app.post("/api/import/run", response_model=Envelope)
    def run_import(payload: ImportRunRequest, request: Request) -> Envelope:
        result = system_from(request).workflows.run_import(
            path=payload.path,
            recursive=payload.recursive,
            domain=payload.domain,
            privacy=payload.privacy,
            inspection_token=payload.inspection_token,
        )
        if result["status"] == "failed":
            return warning(
                "导入工作流未完成",
                result,
                next_actions=[result["error"]["safe_retry"]],
            )
        imported = result["result"]["imported"]
        return success(
            f"导入工作流完成，新增 {imported} 个资料来源",
            result,
            artifacts=result.get("artifacts", []),
        )

    @app.get("/api/workflows", response_model=Envelope)
    def workflow_definitions(request: Request) -> Envelope:
        items = system_from(request).workflows.list_definitions()
        return success("已读取可执行工作流", items)

    @app.get("/api/workflows/runs", response_model=Envelope)
    def workflow_runs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Envelope:
        items = system_from(request).repository.list_workflow_runs(limit)
        return success(f"已读取 {len(items)} 次工作流运行记录", items)

    @app.post("/api/workflows/rebuild-index", response_model=Envelope)
    def rebuild_index(request: Request) -> Envelope:
        result = system_from(request).workflows.rebuild_search_index()
        if result["status"] == "failed":
            return warning("搜索索引重建失败", result)
        return success(f"已重建 {result['indexed_chunks']} 个知识片段的索引", result)

    @app.post("/api/agent/context", response_model=Envelope)
    def agent_context(payload: AgentContextRequest, request: Request) -> Envelope:
        result = system_from(request).agent.prepare_context(
            task=payload.task,
            domain=payload.domain,
            limit=payload.limit,
            include_restricted=payload.include_restricted,
        )
        return success(result["summary"], result)

    @app.get("/api/agent/runs", response_model=Envelope)
    def agent_runs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Envelope:
        items = system_from(request).repository.list_agent_runs(limit)
        return success(f"已读取 {len(items)} 次智能体上下文运行记录", items)

    @app.get("/api/persona", response_model=Envelope)
    def persona(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Envelope:
        items = system_from(request).repository.list_persona_observations(limit)
        return success(f"已读取 {len(items)} 条个人观察", items)

    @app.post("/api/persona", response_model=Envelope)
    def create_persona(payload: PersonaCandidateRequest, request: Request) -> Envelope:
        item = system_from(request).repository.create_persona_candidate(
            payload.observation_type,
            payload.statement,
            payload.evidence_ids,
            payload.confidence,
        )
        return success(
            "个人观察已保存为候选，只有你批准后才会成为长期画像",
            item,
            next_actions=["检查证据和反例，再批准或驳回"],
        )

    @app.post("/api/persona/{observation_id}/review", response_model=Envelope)
    def review_persona(
        observation_id: str,
        payload: ReviewRequest,
        request: Request,
    ) -> Envelope:
        reviewed = system_from(request).repository.review_persona(
            observation_id,
            payload.decision,
            payload.reason,
        )
        if not reviewed:
            raise HTTPException(status_code=404, detail="个人观察不存在")
        return success(f"个人观察已{_decision_label(payload.decision)}")

    @app.get("/api/distillation", response_model=Envelope)
    def distillation_examples(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Envelope:
        items = system_from(request).repository.list_distillation_examples(limit)
        return success(f"已读取 {len(items)} 条蒸馏样本", items)

    @app.post("/api/distillation", response_model=Envelope)
    def create_distillation(
        payload: DistillationCandidateRequest,
        request: Request,
    ) -> Envelope:
        item = system_from(request).repository.create_distillation_candidate(
            example_type=payload.example_type,
            input_text=payload.input_text,
            preferred_output=payload.preferred_output,
            rejected_output=payload.rejected_output,
            rationale=payload.rationale,
            source_ids=payload.source_ids,
            privacy=payload.privacy,
        )
        return success(
            "蒸馏样本已保存为候选",
            item,
            next_actions=["审核样本质量和隐私范围后再批准导出"],
        )

    @app.post("/api/distillation/{example_id}/review", response_model=Envelope)
    def review_distillation(
        example_id: str,
        payload: ReviewRequest,
        request: Request,
    ) -> Envelope:
        reviewed = system_from(request).repository.review_distillation(
            example_id,
            payload.decision,
            payload.reason,
        )
        if not reviewed:
            raise HTTPException(status_code=404, detail="蒸馏样本不存在")
        return success(f"蒸馏样本已{_decision_label(payload.decision)}")

    @app.post("/api/distillation/export", response_model=Envelope)
    def export_distillation(
        payload: DistillationExportRequest,
        request: Request,
    ) -> Envelope:
        result = system_from(request).distillation.export_jsonl(
            approved_only=payload.approved_only,
            dataset_split=payload.dataset_split,
        )
        return success(
            f"已导出 {result['example_count']} 条蒸馏样本",
            result,
            artifacts=[result["path"]],
        )

    @app.get("/api/audit", response_model=Envelope)
    def audit(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Envelope:
        items = system_from(request).repository.audit_events(limit)
        return success(f"已读取 {len(items)} 条审计记录", items)

    @app.post("/api/weflow/exports/discover", response_model=Envelope)
    def discover_weflow_exports(
        payload: WeFlowExportDiscoverRequest,
        request: Request,
    ) -> Envelope:
        try:
            result = system_from(request).weflow.discover_exports(
                records_path=payload.records_path,
                keyword=payload.keyword,
                limit=payload.limit,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success(
            f"发现 {result['existing_sessions']} 个有现存 XLSX 的 WeFlow 会话",
            result,
            next_actions=["勾选需要进入知识库的客户会话，再执行只读结构检查"],
        )

    @app.post("/api/weflow/exports/inspect", response_model=Envelope)
    def inspect_weflow_exports(
        payload: WeFlowExportInspectRequest,
        request: Request,
    ) -> Envelope:
        try:
            result = system_from(request).weflow.inspect_export_selection(
                records_path=payload.records_path,
                session_ids=payload.session_ids,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success(
            f"已检查 {result['selected_sessions']} 个 WeFlow XLSX，会话内容尚未导入",
            result,
            next_actions=["核对会话、消息数量和 restricted 范围后确认导入"],
        )

    @app.post("/api/weflow/exports/import", response_model=Envelope)
    def import_weflow_exports(
        payload: WeFlowExportImportRequest,
        request: Request,
    ) -> Envelope:
        result = system_from(request).customer_workflows.import_weflow_exports(
            records_path=payload.records_path,
            session_ids=payload.session_ids,
            inspection_token=payload.inspection_token,
            privacy=payload.privacy,
        )
        if result["status"] == "failed":
            return warning(
                "WeFlow XLSX 客户会话导入未完成",
                result,
                next_actions=[result["error"]["safe_retry"]],
            )
        imported = result["result"]["imported"]
        return success(
            f"已从 WeFlow XLSX 导入 {imported} 条客户消息",
            result,
            artifacts=result["artifacts"],
        )

    @app.post("/api/weflow/chatlab/inspect", response_model=Envelope)
    def inspect_chatlab(payload: ChatLabInspectRequest, request: Request) -> Envelope:
        try:
            result = system_from(request).weflow.inspect_chatlab_file(
                payload.path,
                session_id=payload.session_id,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success(
            "WeFlow ChatLab 文件检查完成，尚未复制或索引聊天内容",
            result,
            next_actions=["确认会话 ID、消息数量和 restricted 隐私范围后再导入"],
        )

    @app.post("/api/weflow/chatlab/import", response_model=Envelope)
    def import_chatlab(payload: ChatLabImportRequest, request: Request) -> Envelope:
        result = system_from(request).customer_workflows.import_chatlab(
            path=payload.path,
            inspection_token=payload.inspection_token,
            session_id=payload.session_id,
            privacy=payload.privacy,
        )
        if result["status"] == "failed":
            return warning(
                "WeFlow ChatLab 导入未完成",
                result,
                next_actions=[result["error"]["safe_retry"]],
            )
        imported = result["result"]["imported"]
        return success(
            f"已导入 {imported} 条 WeFlow 客户消息",
            result,
            artifacts=result["artifacts"],
        )

    @app.get("/api/customers", response_model=Envelope)
    def customers(
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> Envelope:
        items = system_from(request).customers.list_customers(limit)
        return success(f"已读取 {len(items)} 个微信客户会话", items)

    @app.get("/api/customers/{customer_id}", response_model=Envelope)
    def customer(customer_id: str, request: Request) -> Envelope:
        item = system_from(request).customers.get_customer(customer_id)
        if not item:
            raise HTTPException(status_code=404, detail="客户不存在")
        return success("已读取客户档案", item)

    @app.post("/api/customers/{customer_id}", response_model=Envelope)
    def update_customer(
        customer_id: str,
        payload: CustomerUpdateRequest,
        request: Request,
    ) -> Envelope:
        item = system_from(request).customers.update_customer(
            customer_id,
            company=payload.company,
            stage=payload.stage,
            tags=payload.tags,
            summary=payload.summary,
            review_status=payload.review_status,
        )
        if not item:
            raise HTTPException(status_code=404, detail="客户不存在")
        return success("客户档案已更新", item)

    @app.get("/api/customers/{customer_id}/timeline", response_model=Envelope)
    def customer_timeline(
        customer_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        before: int | None = Query(default=None, ge=0),
        include_restricted: bool = False,
    ) -> Envelope:
        if not system_from(request).customers.get_customer(customer_id):
            raise HTTPException(status_code=404, detail="客户不存在")
        items = system_from(request).customers.timeline(
            customer_id,
            limit=limit,
            before=before,
            include_restricted=include_restricted,
        )
        return success(f"已读取 {len(items)} 条客户时间线消息", items)

    @app.post("/api/customer-messages/search", response_model=Envelope)
    def search_customer_messages(
        payload: CustomerSearchRequest,
        request: Request,
    ) -> Envelope:
        items = system_from(request).customers.search_messages(
            payload.query,
            customer_id=payload.customer_id,
            limit=payload.limit,
            include_restricted=payload.include_restricted,
        )
        return success(f"找到 {len(items)} 条客户聊天证据", items)

    @app.post("/api/customer-reply/context", response_model=Envelope)
    def customer_reply_context(
        payload: CustomerReplyContextRequest,
        request: Request,
    ) -> Envelope:
        result = system_from(request).customer_service.prepare_reply_context(
            customer_id=payload.customer_id,
            task=payload.task,
            recent_limit=payload.recent_limit,
            search_limit=payload.search_limit,
            include_restricted=payload.include_restricted,
        )
        if not result:
            raise HTTPException(status_code=404, detail="客户不存在")
        return success(result["summary"], result)

    @app.get("/api/customer-signals", response_model=Envelope)
    def customer_signals(
        request: Request,
        customer_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> Envelope:
        items = system_from(request).customers.list_signals(customer_id, limit)
        return success(f"已读取 {len(items)} 条客户业务信号", items)

    @app.post("/api/customer-signals", response_model=Envelope)
    def create_customer_signal(
        payload: CustomerSignalRequest,
        request: Request,
    ) -> Envelope:
        item = system_from(request).customers.create_signal(
            customer_id=payload.customer_id,
            signal_type=payload.signal_type,
            statement=payload.statement,
            status=payload.status,
            due_at=payload.due_at,
            evidence_message_ids=payload.evidence_message_ids,
            confidence=payload.confidence,
        )
        if not item:
            raise HTTPException(status_code=404, detail="客户不存在")
        return success(
            "客户需求、承诺或待办已保存为候选，等待用户审核",
            item,
            next_actions=["核对聊天证据后批准或驳回"],
        )

    @app.post("/api/customer-signals/{signal_id}/review", response_model=Envelope)
    def review_customer_signal(
        signal_id: str,
        payload: ReviewRequest,
        request: Request,
    ) -> Envelope:
        reviewed = system_from(request).customers.review_signal(
            signal_id,
            payload.decision,
            payload.reason,
        )
        if not reviewed:
            raise HTTPException(status_code=404, detail="客户业务信号不存在")
        return success(f"客户业务信号已{_decision_label(payload.decision)}")

    web_dist = Path(resolved_settings.web_dist)
    assets = web_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")
        index = web_dist / "index.html"
        if not index.exists():
            raise HTTPException(
                status_code=503,
                detail="管理界面尚未构建，请先在 web 目录执行 npm run build",
            )
        return FileResponse(index)

    return app


def _decision_label(decision: str) -> str:
    return "批准" if decision == "approved" else "驳回"


app = create_app()
