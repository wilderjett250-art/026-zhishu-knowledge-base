import socket
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pkas.auto_promotion import (
    AutoPromotionRequest,
    AutoPromotionRunRequest,
    AutoPromotionService,
)
from pkas.capability_registry import CapabilityRegistry
from pkas.client_config import (
    ClientConfigConflict,
    ClientConfigError,
    ClientConfigTransactionService,
)
from pkas.config import Settings, get_settings
from pkas.content_taxonomy import (
    TaxonomyRestore,
    TaxonomyUpdate,
    load_taxonomy,
    restore_system_taxonomy,
    save_taxonomy,
)
from pkas.customer_service import CustomerReviewRequired
from pkas.directory_summary import (
    CatalogOverviewRequest,
    ClassificationEdit,
    DirectorySummaryRequest,
    DirectorySummaryRunRequest,
    DirectorySummaryService,
)
from pkas.document_extraction import DOCUMENT_PIPELINE_VERSION
from pkas.document_policy import (
    DocumentPolicyRequest,
    disable_enhancement,
    document_policy,
)
from pkas.everything_scanner import status as everything_status
from pkas.foundation import FoundationService, suggested_scopes
from pkas.ingest import ImportBoundaryError
from pkas.intake import (
    DEFAULT_RULES,
    IntakeBrowseRequest,
    IntakePolicy,
    IntakeRequest,
    IntakeService,
)
from pkas.machine_catalog import MachineCatalogStart, eligible_scopes
from pkas.mcp_inspector import McpInspectorConflict, McpInspectorError, McpInspectorService
from pkas.processing_profiles import (
    ProcessingProfileUpdate,
    list_processing_profiles,
    load_processing_profile,
    save_processing_profile,
)
from pkas.profile_service import ProfileConflict, ProfileError, ProfileNotFound
from pkas.project_map import (
    ProjectMapCreateRequest,
    ProjectMapPromotionRequest,
    ProjectMapRunRequest,
    ProjectMapService,
)
from pkas.runtime_manager import RuntimeManager
from pkas.schemas import (
    AgentCloseoutRequest,
    AgentContextRequest,
    AgentRunRequest,
    CapabilityProfileReplaceRequest,
    CapabilityProfileRequest,
    CatalogSearchRequest,
    ChatLabImportRequest,
    ChatLabInspectRequest,
    ClientConfigApplyRequest,
    ClientConfigPreviewRequest,
    ClientConfigRollbackRequest,
    CustomerReplyContextRequest,
    CustomerSearchRequest,
    CustomerSignalRequest,
    CustomerUpdateRequest,
    DistillationCandidateRequest,
    DistillationExportRequest,
    Envelope,
    ImportInspectRequest,
    ImportRunRequest,
    McpProbeRequest,
    PersonaCandidateRequest,
    PersonalTimelineBuildRequest,
    RagEvalCaseRequest,
    RagEvalJudgmentRequest,
    RagEvalRejectRequest,
    RagEvalReviewRequest,
    RagEvalRunRequest,
    ReviewRequest,
    SearchRequest,
    WeFlowExportDiscoverRequest,
    WeFlowExportImportRequest,
    WeFlowExportInspectRequest,
    WeFlowManualSyncRequest,
)
from pkas.search_gateway import UnifiedSearchRequest, unified_search
from pkas.summary_jobs import SummaryJobs
from pkas.summary_routes import router as summary_router
from pkas.system import KnowledgeSystem
from pkas.thread_journal import ThreadJournalScheduler, ThreadJournalService
from pkas.usage_metrics import UsageMetrics


class RuntimeAction(BaseModel):
    action: Literal["start", "stop"]
    confirmed: Literal[True]
    confirm_cloud: bool = False


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


def capability_rag_status(system: KnowledgeSystem) -> dict[str, Any]:
    """Keep the control plane responsive when the optional vector sidecar is offline."""
    parsed = urlparse(system.settings.qdrant_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6333
    try:
        with socket.create_connection((host, port), timeout=0.25):
            coverage = system.rag.vector_index.coverage()
            qdrant: dict[str, Any] = {
                "status": "ready",
                "points": int(coverage.get("indexed", 0)),
            }
            try:
                client = system.rag.vector_index._get_client()
                if system.rag.vector_index._collection_exists(client):
                    info = client.get_collection(system.settings.qdrant_collection)
                    vectors = info.config.params.vectors
                    qdrant.update(
                        {
                            "points": int(info.points_count or 0),
                            "dimension": int(getattr(vectors, "size", 0) or 0),
                        }
                    )
                else:
                    qdrant["status"] = "not_indexed"
            except Exception:
                qdrant = {
                    "status": "warning",
                    "points": int(coverage.get("indexed", 0)),
                    "warning": "Qdrant 轻量状态读取失败；全文检索仍可使用。",
                }
            return {"mcp_enabled": False, "qdrant": qdrant}
    except OSError:
        coverage = system.rag.vector_index.coverage()
        return {
            "mcp_enabled": False,
            "qdrant": {
                "status": "offline",
                "points": int(coverage.get("indexed", 0)),
                "warning": "Qdrant 当前未监听；本地全文检索仍可使用。",
            },
        }


def capability_knowledge_stats(system: KnowledgeSystem) -> dict[str, Any]:
    """Read only the three counters rendered by the capability center."""
    with system.database.connect() as connection:
        counts = {
            "knowledge_chunks": int(
                connection.execute(
                    """SELECT COUNT(*) FROM chunks c JOIN sources s ON s.id=c.source_id
                    WHERE s.status='indexed'
                      AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')"""
                ).fetchone()[0]
            ),
            "workflow_runs": int(
                connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
            ),
            "agent_runs": int(connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]),
            "profiles": int(
                connection.execute("SELECT COUNT(*) FROM capability_profiles").fetchone()[0]
            ),
        }
    return {"counts": counts}


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        system = KnowledgeSystem.create(resolved_settings)
        app.state.system = system
        app.state.mcp_inspector = McpInspectorService(system.settings.client_home)
        app.state.runtime_manager = RuntimeManager(resolved_settings)
        app.state.usage_metrics = UsageMetrics(resolved_settings.data_root)
        app.state.intake = IntakeService(system)
        app.state.summary_jobs = SummaryJobs(resolved_settings)
        app.state.thread_journal = ThreadJournalScheduler(
            ThreadJournalService(resolved_settings, system.repository),
            resolved_settings,
        )
        app.state.thread_journal.start()
        try:
            yield
        finally:
            app.state.thread_journal.close()
            system.machine_catalog.close()
            app.state.summary_jobs.close()
            app.state.intake.close()
            app.state.runtime_manager.close()

    app = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.include_router(summary_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def observe_api(request: Request, call_next):
        started = time.monotonic()
        outcome = "error"
        try:
            response = await call_next(request)
            outcome = "success" if response.status_code < 400 else "error"
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", "")
            if path.startswith("/api/") and not path.startswith(
                ("/api/runtime", "/api/health", "/api/core/readiness", "/api/foundation")
            ):
                metrics = getattr(request.app.state, "usage_metrics", None)
                if metrics:
                    from starlette.concurrency import run_in_threadpool

                    await run_in_threadpool(
                        metrics.record, "api", path, outcome, time.monotonic() - started
                    )

    @app.get("/api/runtime/ping")
    def runtime_ping():
        return {"service": "pkas-runtime", "status": "ready"}

    @app.get("/api/runtime/overview", response_model=Envelope)
    def runtime_overview(request: Request):
        data = request.app.state.runtime_manager.status()
        data["thread_journal"] = request.app.state.thread_journal.status()
        try:
            data["usage"] = request.app.state.usage_metrics.summary()
        except Exception:
            data["usage"] = {
                "calls": None,
                "success_rate": None,
                "operations": [],
                "coverage": "使用统计暂不可用，不代表零调用",
            }
        with request.app.state.system.database.connect() as connection:
            queues = {
                table: {
                    row[0]: row[1]
                    for row in connection.execute(
                        f"SELECT status,COUNT(*) FROM {table} GROUP BY status"
                    ).fetchall()
                }
                for table in ("index_outbox", "agent_jobs", "workflow_runs")
            }
            vector_settings = request.app.state.system.rag.vector_index.settings
            scope_clause = ""
            if vector_settings.embedding_scope == "selected_l3":
                scope_clause = (
                    " AND json_extract(s.metadata_json, "
                    "'$.requested_processing_level') = 'L3'"
                )
            restricted_clause = ""
            if not vector_settings.embedding_allow_restricted_remote_processing:
                restricted_clause = " AND c.privacy <> 'restricted'"
            actionable = {
                row[0]: row[1]
                for row in connection.execute(
                    f"""SELECT o.status,COUNT(*)
                    FROM index_outbox o
                    JOIN chunks c ON c.id=o.entity_id
                    JOIN sources s ON s.id=c.source_id
                    WHERE o.entity_type='chunk' AND o.operation='upsert'
                      AND s.status='indexed'
                      AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                      {scope_clause}{restricted_clause}
                    GROUP BY o.status"""
                ).fetchall()
            }
            all_outbox = queues["index_outbox"]
            queues["index_outbox_actionable"] = actionable
            queues["index_outbox_background"] = {
                status: max(0, int(count) - int(actionable.get(status, 0)))
                for status, count in all_outbox.items()
            }
            data["queues"] = queues
        return success("已读取统一运行中心", data)

    @app.post("/api/runtime/services/{service}", response_model=Envelope)
    def runtime_control(service: str, payload: RuntimeAction, request: Request):
        origin = request.headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:8765", "http://localhost:8765"}:
            raise HTTPException(status_code=403, detail="只允许本机管理界面控制服务")
        try:
            result = request.app.state.runtime_manager.control(
                service, payload.action, payload.confirm_cloud
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return success("已提交运行操作，请以服务状态为准", result)

    def system_from(request: Request) -> KnowledgeSystem:
        return request.app.state.system

    def mcp_inspector_from(request: Request) -> McpInspectorService:
        return request.app.state.mcp_inspector

    def agent_route(method: str, path: str, **kwargs):
        """Do not expose the legacy PKAS Agent API in the normal product."""

        register = getattr(app, method)

        def decorate(function):
            if resolved_settings.agent_runtime_enabled:
                return register(path, **kwargs)(function)
            return function

        return decorate

    @app.get("/api/health", response_model=Envelope)
    def health(request: Request) -> Envelope:
        system = system_from(request)
        details: dict[str, Any] = dict(system.database.health())
        details["app_version"] = system.settings.app_version
        details["document_extraction"] = {
            "pipeline": DOCUMENT_PIPELINE_VERSION,
            **document_policy(system.settings),
            "restricted_remote_enabled": (
                system.settings.document_allow_restricted_remote_processing
            ),
        }
        details["index_outbox"] = system.outbox.stats()
        details["backup"] = system.backup.status()
        return success("个人知识系统运行正常", details)

    @app.get("/api/settings/document-parsing", response_model=Envelope)
    def document_parsing_status(request: Request) -> Envelope:
        return success("已读取文档解析设置", document_policy(system_from(request).settings))

    @app.post("/api/settings/document-parsing", response_model=Envelope)
    def document_parsing_update(payload: DocumentPolicyRequest, request: Request) -> Envelope:
        if payload.ai_enhancement_enabled:
            raise HTTPException(status_code=409, detail="Luna 文档增强尚未接入，不能启用。")
        return success(
            "已关闭增强；从下一份文档开始使用本地解析，不中断正在解析的文件。",
            disable_enhancement(system_from(request).settings),
        )

    @app.get("/api/dashboard", response_model=Envelope)
    def dashboard(request: Request) -> Envelope:
        stats = system_from(request).repository.stats()
        return success("已读取个人知识系统概览", stats)

    @app.get("/api/core/readiness", response_model=Envelope)
    def core_readiness(request: Request) -> Envelope:
        report = system_from(request).readiness.report()
        if report["status"] != "ready_for_local_trial":
            return warning("核心能力仍有阻塞门禁", report, next_actions=report["next_required"])
        return success("核心能力已达到本机试用门禁", report, next_actions=report["next_required"])

    @app.get("/api/core/readiness/{section}", response_model=Envelope)
    def readiness_section(section: str, request: Request) -> Envelope:
        service = system_from(request).readiness
        if section not in service.section_names:
            raise HTTPException(status_code=404, detail="未知的系统检查项")
        return success("已读取系统检查项", service.section(section))

    @app.get("/api/capabilities/overview", response_model=Envelope)
    def capability_overview(request: Request) -> Envelope:
        system = system_from(request)
        registry = CapabilityRegistry(system.settings.client_home)
        overview = registry.overview(
            knowledge_stats=capability_knowledge_stats(system),
            rag_status=capability_rag_status(system),
        )
        if overview["warnings"]:
            return warning("能力中心已读取，但部分客户端配置需要检查", overview)
        return success("已读取本机 AI 能力与客户端接入状态", overview)

    @app.get("/api/capabilities/clients/config", response_model=Envelope)
    def client_config_status(request: Request) -> Envelope:
        system = system_from(request)
        service = ClientConfigTransactionService(
            system.settings,
            system.settings.client_home,
        )
        return success("已读取客户端配置事务状态", service.client_statuses())

    @app.get(
        "/api/capabilities/mcp/{client_id}/{server_id}/preview",
        response_model=Envelope,
    )
    def preview_mcp_probe(client_id: str, server_id: str, request: Request) -> Envelope:
        try:
            result = mcp_inspector_from(request).preview(client_id, server_id)
        except McpInspectorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("已生成脱敏 MCP 探测预览；尚未启动任何进程", result)

    @app.post(
        "/api/capabilities/mcp/{client_id}/{server_id}/probe",
        response_model=Envelope,
    )
    async def probe_mcp_server(
        client_id: str,
        server_id: str,
        payload: McpProbeRequest,
        request: Request,
    ) -> Envelope:
        if not payload.confirmed:
            raise HTTPException(status_code=400, detail="必须明确确认同一份脱敏预览后才能探测。")
        try:
            result = await mcp_inspector_from(request).probe(
                client_id,
                server_id,
                preview_token=payload.preview_token,
                timeout_seconds=payload.timeout_seconds,
            )
        except McpInspectorConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except McpInspectorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("MCP 单次能力探测完成，临时进程已回收", result)

    @app.post(
        "/api/capabilities/clients/{client_id}/config/preview",
        response_model=Envelope,
    )
    def preview_client_config(
        client_id: str,
        payload: ClientConfigPreviewRequest,
        request: Request,
    ) -> Envelope:
        system = system_from(request)
        service = ClientConfigTransactionService(
            system.settings,
            system.settings.client_home,
        )
        try:
            result = service.preview(client_id, enabled=payload.enabled)
        except ClientConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("已生成脱敏配置差异；尚未写入客户端", result)

    @app.post(
        "/api/capabilities/clients/{client_id}/config/apply",
        response_model=Envelope,
    )
    async def apply_client_config(
        client_id: str,
        payload: ClientConfigApplyRequest,
        request: Request,
    ) -> Envelope:
        if not payload.confirmed:
            raise HTTPException(status_code=400, detail="必须明确确认同一份脱敏差异后才能写入。")
        system = system_from(request)
        service = ClientConfigTransactionService(
            system.settings,
            system.settings.client_home,
        )
        try:
            result = await service.apply(
                client_id,
                enabled=payload.enabled,
                preview_token=payload.preview_token,
            )
        except ClientConfigConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ClientConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("客户端配置已原子写入并通过验证", result)

    @app.get(
        "/api/capabilities/clients/{client_id}/config/backups",
        response_model=Envelope,
    )
    def list_client_config_backups(client_id: str, request: Request) -> Envelope:
        system = system_from(request)
        service = ClientConfigTransactionService(
            system.settings,
            system.settings.client_home,
        )
        try:
            result = service.list_backups(client_id)
        except ClientConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("已读取当前用户受保护的客户端配置备份", result)

    @app.post(
        "/api/capabilities/clients/{client_id}/config/rollback",
        response_model=Envelope,
    )
    def rollback_client_config(
        client_id: str,
        payload: ClientConfigRollbackRequest,
        request: Request,
    ) -> Envelope:
        if not payload.confirmed:
            raise HTTPException(status_code=400, detail="必须明确确认后才能恢复客户端配置。")
        system = system_from(request)
        service = ClientConfigTransactionService(
            system.settings,
            system.settings.client_home,
        )
        try:
            result = service.rollback(client_id, payload.backup_id)
        except ClientConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("客户端配置已从受保护备份恢复", result)

    @app.get("/api/capabilities/profiles", response_model=Envelope)
    def list_capability_profiles(request: Request) -> Envelope:
        profiles = system_from(request).profiles.list_profiles()
        return success(f"已读取 {len(profiles)} 个能力 Profile", profiles)

    @app.get("/api/capabilities/profiles/{profile_id}", response_model=Envelope)
    def get_capability_profile(profile_id: str, request: Request) -> Envelope:
        try:
            profile = system_from(request).profiles.get_profile(profile_id)
        except ProfileNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return success("已读取能力 Profile", profile)

    @app.post("/api/capabilities/profiles", response_model=Envelope)
    def create_capability_profile(
        payload: CapabilityProfileRequest,
        request: Request,
    ) -> Envelope:
        try:
            profile = system_from(request).profiles.create_profile(payload.model_dump())
        except ProfileConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("能力 Profile 已创建；尚未写入任何客户端配置", profile)

    @app.put("/api/capabilities/profiles/{profile_id}", response_model=Envelope)
    def replace_capability_profile(
        profile_id: str,
        payload: CapabilityProfileReplaceRequest,
        request: Request,
    ) -> Envelope:
        values = payload.model_dump(exclude={"expected_revision"})
        try:
            profile = system_from(request).profiles.replace_profile(
                profile_id,
                values,
                expected_revision=payload.expected_revision,
            )
        except ProfileNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProfileConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("能力 Profile 已更新；尚未写入任何客户端配置", profile)

    @app.post("/api/search", response_model=Envelope)
    def search(payload: SearchRequest, request: Request) -> Envelope:
        response = unified_search(
            system_from(request), UnifiedSearchRequest(**payload.model_dump())
        )
        result = success(
            f"找到 {len(response['results'])} 条带来源的知识片段（{response['mode']}）",
            response["results"],
        )
        if response["warnings"]:
            result.status = "warning"
            result.next_actions = response["warnings"]
        return result

    @app.post("/api/rag/search-debug", response_model=Envelope)
    def rag_search_debug(payload: SearchRequest, request: Request) -> Envelope:
        data = unified_search(system_from(request), UnifiedSearchRequest(**payload.model_dump()))
        result = success("已完成可解释检索", data)
        if data["warnings"]:
            result.status = "warning"
            result.next_actions = data["warnings"]
        return result

    @app.get("/api/rag/status", response_model=Envelope)
    def rag_status(request: Request) -> Envelope:
        data = system_from(request).rag.status()
        return success("已读取真实向量索引状态", data)

    @app.get("/api/rag/vector-map", response_model=Envelope)
    def rag_vector_map(
        request: Request,
        limit: int = Query(default=300, ge=1, le=500),
    ) -> Envelope:
        try:
            data = system_from(request).rag.vector_map(limit)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return success(f"已投影 {data['sampled']} 个真实向量片段", data)

    @app.get("/api/rag/eval/cases", response_model=Envelope)
    def rag_eval_cases(request: Request) -> Envelope:
        items = system_from(request).rag.list_cases()
        return success(f"已读取 {len(items)} 个黄金测评样本", items)

    @app.get("/api/rag/eval/review/progress", response_model=Envelope)
    def rag_eval_review_progress(request: Request) -> Envelope:
        data = system_from(request).rag.review_progress()
        return success(f"人工金标进度 {data['eligible']}/{data['total']}", data)

    @app.get("/api/rag/eval/fusion-tuning-status", response_model=Envelope)
    def rag_eval_fusion_tuning_status(request: Request) -> Envelope:
        data = system_from(request).rag.fusion_tuning_status()
        if not data["may_apply"]:
            return warning("融合权重等待人工金标", data, next_actions=[data["reason"]])
        return success("融合权重已达到调优门槛", data)

    @app.post("/api/rag/eval/fusion-tune", response_model=Envelope)
    def rag_eval_fusion_tune(request: Request) -> Envelope:
        data = system_from(request).rag.tune_fusion_weights()
        if data["status"] != "completed":
            return warning("融合权重等待人工金标", data, next_actions=[data["reason"]])
        return success("融合权重离线搜索完成", data)

    @app.get(
        "/api/rag/eval/cases/{case_id}/review-context",
        response_model=Envelope,
    )
    def rag_eval_review_context(
        case_id: str,
        request: Request,
        limit: int = Query(default=10, ge=5, le=20),
        rerank_mode: str = Query(default="auto", pattern="^(auto|never|always)$"),
    ) -> Envelope:
        try:
            data = system_from(request).rag.review_context(
                case_id, limit=limit, rerank_mode=rerank_mode
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return success(f"已准备 {data['scope_count']} 个待人工判断来源", data)

    @app.post("/api/rag/eval/cases/{case_id}/review", response_model=Envelope)
    def finalize_rag_eval_review(
        case_id: str, payload: RagEvalReviewRequest, request: Request
    ) -> Envelope:
        try:
            item = system_from(request).rag.finalize_review(case_id, **payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("人工金标复核已完成并通过正式门禁", item)

    @app.post("/api/rag/eval/cases/{case_id}/reject", response_model=Envelope)
    def reject_rag_eval_case(
        case_id: str, payload: RagEvalRejectRequest, request: Request
    ) -> Envelope:
        try:
            item = system_from(request).rag.reject_case(case_id, payload.reason_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("问题已人工剔除；需要补充一条新的合格金标题", item)

    @app.post("/api/rag/eval/cases", response_model=Envelope)
    def create_rag_eval_case(payload: RagEvalCaseRequest, request: Request) -> Envelope:
        try:
            item = system_from(request).rag.create_case(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return success("黄金测评样本已保存", item)

    @app.post("/api/rag/eval/cases/{case_id}/judgments", response_model=Envelope)
    def update_rag_eval_judgments(
        case_id: str, payload: RagEvalJudgmentRequest, request: Request
    ) -> Envelope:
        item = system_from(request).rag.upsert_judgments(
            case_id, [value.model_dump() for value in payload.judgments]
        )
        return success("逐来源相关性标注已保存", item)

    @app.get("/api/rag/eval/runs", response_model=Envelope)
    def rag_eval_runs(request: Request) -> Envelope:
        items = system_from(request).rag.list_runs()
        return success(f"已读取 {len(items)} 次测评", items)

    @app.get("/api/rag/eval/silver/latest", response_model=Envelope)
    def latest_rag_silver_gate(request: Request) -> Envelope:
        item = system_from(request).rag.latest_silver_gate()
        if item is None:
            return warning("尚未运行银标召回门禁")
        return success("已读取最新银标召回门禁", item)

    @app.get("/api/rag/eval/silver/latest/report")
    def download_latest_rag_silver_gate(request: Request) -> FileResponse:
        path = system_from(request).rag.latest_silver_gate_path()
        if path is None:
            raise HTTPException(status_code=404, detail="尚未运行银标召回门禁")
        return FileResponse(
            path,
            media_type="application/json",
            filename=path.name,
        )

    @app.post("/api/rag/eval/run", response_model=Envelope)
    def run_rag_eval(payload: RagEvalRunRequest, request: Request) -> Envelope:
        data = system_from(request).rag.run_eval(
            payload.top_k, payload.rerank_mode, payload.retrieval_mode
        )
        if data["status"] == "warning":
            return warning(data["warning"], data)
        return success(f"RAG 黄金集测评完成（{data['case_count']} 题）", data)

    @app.get("/api/sources", response_model=Envelope)
    def sources(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Envelope:
        items = system_from(request).repository.list_sources(limit, status="indexed")
        return success(f"已读取 {len(items)} 个资料来源", items)

    @app.get("/api/foundation/intake", response_model=Envelope)
    def intake_recent(request: Request) -> Envelope:
        return success("分类接入记录", request.app.state.intake.recent())

    @app.get("/api/foundation/everything", response_model=Envelope)
    def everything_component(request: Request) -> Envelope:
        data = everything_status(system_from(request).settings.project_root)
        return success("Everything 组件状态", data)

    @app.post("/api/foundation/intake/browse", response_model=Envelope)
    def intake_browse(payload: IntakeBrowseRequest, request: Request) -> Envelope:
        try:
            data = request.app.state.intake.browse(payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        return success("已读取目录第一层；尚未递归扫描或入库", data)

    @app.get("/api/foundation/directory-summaries", response_model=Envelope)
    def directory_summary_status(request: Request) -> Envelope:
        service = DirectorySummaryService(system_from(request).settings)
        return success("已读取目录摘要策略；默认不调用云端", service.status())

    @app.post("/api/foundation/directory-summaries/catalog-overview", response_model=Envelope)
    def directory_catalog_overview(payload: CatalogOverviewRequest, request: Request) -> Envelope:
        try:
            data = DirectorySummaryService(system_from(request).settings).create_catalog_overview(
                payload
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(
                409, str(exc) if isinstance(exc, ValueError) else "无法读取A库索引"
            ) from None
        return success("已从A库生成全盘目录概览计划；尚未调用模型", data)

    @app.get("/api/foundation/content-categories", response_model=Envelope)
    def content_categories(request: Request) -> Envelope:
        return success("内容用途分类", load_taxonomy(system_from(request).settings.data_root))

    @app.post("/api/foundation/content-categories", response_model=Envelope)
    def content_categories_save(payload: TaxonomyUpdate, request: Request) -> Envelope:
        try:
            result = save_taxonomy(system_from(request).settings.data_root, payload)
        except (ValueError, OSError):
            raise HTTPException(409, "分类无法保存或已被修改，请刷新后重试") from None
        return success("分类已保存，下次检查使用新规则；旧记录保留原分类", result)

    @app.post("/api/foundation/content-categories/restore", response_model=Envelope)
    def content_categories_restore(payload: TaxonomyRestore, request: Request) -> Envelope:
        try:
            result = restore_system_taxonomy(system_from(request).settings.data_root, payload)
        except (ValueError, OSError):
            raise HTTPException(409, "系统分类无法恢复或已被修改，请刷新后重试") from None
        return success("已恢复系统基准分类；修改前版本仍保留为恢复点", result)

    @app.get("/api/foundation/directory-summaries/{job_id}", response_model=Envelope)
    def directory_summary_read(job_id: str, request: Request) -> Envelope:
        try:
            result = DirectorySummaryService(system_from(request).settings).read(job_id)
        except (ValueError, OSError):
            raise HTTPException(404, "检查记录不存在") from None
        return success("已读取检查记录", result)

    @app.post("/api/foundation/auto-promotion", response_model=Envelope)
    def auto_promotion_create(payload: AutoPromotionRequest, request: Request) -> Envelope:
        try:
            result = AutoPromotionService(system_from(request).settings).create(payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(
                409, str(exc) if isinstance(exc, ValueError) else "自动选择计划无法保存"
            ) from None
        return success("已生成自动选择计划；尚未调用Luna，也未读取原文件", result)

    @app.get("/api/foundation/auto-promotion/latest", response_model=Envelope)
    def auto_promotion_latest(request: Request) -> Envelope:
        result = AutoPromotionService(system_from(request).settings).latest()
        return success("已读取最近完成的自动选择计划", result)

    @app.get("/api/foundation/auto-promotion/{job_id}", response_model=Envelope)
    def auto_promotion_read(job_id: str, request: Request) -> Envelope:
        try:
            result = AutoPromotionService(system_from(request).settings).read(job_id)
        except (ValueError, OSError):
            raise HTTPException(404, "自动选择计划不存在") from None
        return success("已读取自动选择计划", result)

    @app.post("/api/foundation/auto-promotion/{job_id}/run", response_model=Envelope)
    def auto_promotion_run(
        job_id: str, payload: AutoPromotionRunRequest, request: Request
    ) -> Envelope:
        try:
            result = AutoPromotionService(system_from(request).settings).run(job_id, payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(
                409, str(exc) if isinstance(exc, ValueError) else "自动选择任务无法保存"
            ) from None
        return success("Luna已完成本批目录级建议；未直接入库", result)

    @app.post("/api/foundation/project-map", response_model=Envelope)
    def project_map_create(payload: ProjectMapCreateRequest, request: Request) -> Envelope:
        try:
            result = ProjectMapService(system_from(request).settings).create(payload)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(
                409, str(exc) if isinstance(exc, ValueError) else "项目总览候选无法生成"
            ) from None
        return success("已从已验证的文件理解档案生成项目候选；尚未调用模型", result)

    @app.get("/api/foundation/project-map/latest", response_model=Envelope)
    def project_map_latest(request: Request) -> Envelope:
        return success(
            "已读取最近项目总览候选", ProjectMapService(system_from(request).settings).latest()
        )

    @app.get("/api/foundation/project-map/{project_map_id}", response_model=Envelope)
    def project_map_read(project_map_id: str, request: Request) -> Envelope:
        try:
            result = ProjectMapService(system_from(request).settings).read(project_map_id)
        except (ValueError, OSError):
            raise HTTPException(404, "项目总览候选不存在") from None
        return success("已读取项目总览候选", result)

    @app.post("/api/foundation/project-map/{project_map_id}/refresh", response_model=Envelope)
    def project_map_refresh(project_map_id: str, request: Request) -> Envelope:
        try:
            result = ProjectMapService(system_from(request).settings).refresh(project_map_id)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(
                409, str(exc) if isinstance(exc, ValueError) else "项目总览增量刷新无法保存"
            ) from None
        return success("已增量核对已验证资料；变化项目需重新生成项目卡", result)

    @app.post(
        "/api/foundation/project-map/{project_map_id}/promote-preview",
        response_model=Envelope,
    )
    def project_map_promote_preview(
        project_map_id: str, payload: ProjectMapPromotionRequest, request: Request
    ) -> Envelope:
        try:
            selection = ProjectMapService(system_from(request).settings).promotion_selection(
                project_map_id, payload
            )
            result = request.app.state.intake.preview_classified(selection)
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(
                409, str(exc) if isinstance(exc, ValueError) else "项目入库预览无法生成"
            ) from None
        return success("已按项目候选生成入库预览；尚未写入知识库", result)

    @app.post("/api/foundation/project-map/{project_map_id}/run", response_model=Envelope)
    def project_map_run(
        project_map_id: str, payload: ProjectMapRunRequest, request: Request
    ) -> Envelope:
        try:
            result = ProjectMapService(system_from(request).settings).run(project_map_id, payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from None
        return success("Luna已完成本批项目候选理解；未进入普通检索", result)

    @app.post(
        "/api/foundation/directory-summaries/{job_id}/classification", response_model=Envelope
    )
    def directory_classification_edit(
        job_id: str, payload: ClassificationEdit, request: Request
    ) -> Envelope:
        try:
            result = DirectorySummaryService(system_from(request).settings).edit_classification(
                job_id, payload
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(
                409, str(exc) if isinstance(exc, ValueError) else "记录无法保存"
            ) from None
        return success("文件分类已更新", result)

    @app.post("/api/foundation/directory-summaries/preview", response_model=Envelope)
    def directory_summary_preview(payload: DirectorySummaryRequest, request: Request) -> Envelope:
        try:
            data = DirectorySummaryService(system_from(request).settings).preview(payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        return success("目录检查记录已生成；已抽样正文，未调用模型", data)

    @app.post("/api/foundation/directory-summaries/{job_id}/run", response_model=Envelope)
    def directory_summary_run(
        job_id: str, payload: DirectorySummaryRunRequest, request: Request
    ) -> Envelope:
        try:
            data = DirectorySummaryService(system_from(request).settings).run(job_id, payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from None
        return success("目录摘要任务已提交", data)

    @app.post("/api/foundation/intake-policy/{job_id}", response_model=Envelope)
    def intake_policy(job_id: str, payload: IntakePolicy, request: Request) -> Envelope:
        try:
            data = request.app.state.intake.policy(job_id, payload)
            return success("已更新处理方案，尚未入库", data)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, "当前任务不可修改方案，请重新完成范围预览") from exc

    @app.get("/api/foundation/processing-profiles", response_model=Envelope)
    def processing_profiles(request: Request) -> Envelope:
        return success(
            "已读取四种资料处理方案",
            list_processing_profiles(system_from(request).settings),
        )

    @app.put("/api/foundation/processing-profile", response_model=Envelope)
    def processing_profile_update(
        payload: ProcessingProfileUpdate, request: Request
    ) -> Envelope:
        try:
            data = save_processing_profile(system_from(request).settings, payload)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return success("资料处理方案已保存；不会自动开始扫描", data)

    @app.post("/api/foundation/intake/preview", response_model=Envelope)
    def intake_preview(payload: IntakeRequest, request: Request) -> Envelope:
        try:
            # The browser sends explicit rules after the profile chooser is
            # loaded.  Older clients omit them; use the saved local profile
            # instead of silently reverting to the hard-coded defaults.
            if payload.rules == DEFAULT_RULES:
                current = load_processing_profile(system_from(request).settings)
                payload = payload.model_copy(update={"rules": current["rules"]})
            data = request.app.state.intake.preview(payload)
            return success("已开始有界目录预览，尚未入库", data)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, "目录无效、被排除或已有任务运行，请检查路径和状态") from exc

    @app.get("/api/foundation/intake/{job_id}", response_model=Envelope)
    def intake_status(job_id: str, request: Request, offset: int = Query(0, ge=0)) -> Envelope:
        try:
            return success("分类接入状态", request.app.state.intake.view(job_id, offset))
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.post("/api/foundation/intake/{job_id}/{action}", response_model=Envelope)
    def intake_action(
        job_id: str,
        action: Literal["confirm", "cancel", "split_l3"],
        request: Request,
        confirmed: bool = Query(False),
        confirmed_vector: bool = Query(False),
    ) -> Envelope:
        try:
            service = request.app.state.intake
            if action == "confirm" and confirmed_vector and service.requires_vector(job_id):
                request.app.state.runtime_manager.ensure_qdrant_ready()
            data = {
                "confirm": lambda: service.run(job_id, confirmed, confirmed_vector),
                "cancel": lambda: service.cancel(job_id),
                "split_l3": lambda: service.split_semantic(job_id),
            }[action]()
            return success("已提交操作", data)
        except (ValueError, OSError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else "任务无法读取"
            raise HTTPException(409, detail) from exc

    @app.get("/api/foundation/scopes", response_model=Envelope)
    def foundation_scopes() -> Envelope:
        return success("默认范围建议，尚未授权或扫描", suggested_scopes())

    @app.get("/api/foundation/auto-sources", response_model=Envelope)
    def foundation_auto_sources(request: Request) -> Envelope:
        scopes = eligible_scopes()
        return success(
            "已准备自动资料范围；尚未开始扫描",
            {
                "scopes": scopes,
                "policy": (
                    "C盘只处理用户常用目录；其他本地固定盘按根目录建立A库索引；"
                    "系统目录、缓存、依赖、构建产物和敏感路径自动排除。"
                ),
                "profile": load_processing_profile(system_from(request).settings),
                "requires_path_input": False,
                "scan_started": False,
            },
        )

    @app.get("/api/foundation/machine-catalog", response_model=Envelope)
    def machine_catalog_status(request: Request) -> Envelope:
        service = system_from(request).machine_catalog
        data = service.latest()
        if data is None:
            return warning("尚未建立全机A库索引", {"state": "not_started"})
        return success("已读取全机A库索引状态", data)

    @app.post("/api/foundation/machine-catalog/start", response_model=Envelope)
    def machine_catalog_start(payload: MachineCatalogStart, request: Request) -> Envelope:
        try:
            data = system_from(request).machine_catalog.start(confirmed=payload.confirmed)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return success("全机A库索引已在后台启动；退出知枢会安全暂停", data)

    @app.post("/api/foundation/machine-catalog/{job_id}/resume", response_model=Envelope)
    def machine_catalog_resume(job_id: str, request: Request) -> Envelope:
        try:
            data = system_from(request).machine_catalog.resume(job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return success("全机A库索引已继续", data)

    @app.post("/api/foundation/machine-catalog/{job_id}/pause", response_model=Envelope)
    def machine_catalog_pause(job_id: str, request: Request) -> Envelope:
        try:
            data = system_from(request).machine_catalog.pause(job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return success("全机A库索引已暂停，进度已保留", data)

    @app.get("/api/foundation/machine-catalog/search", response_model=Envelope)
    def machine_catalog_search(
        request: Request,
        query: str = Query(min_length=1, max_length=500),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Envelope:
        data = system_from(request).machine_catalog.search(query, limit)
        return success(f"A库找到 {len(data)} 个文件", data)

    @app.post("/api/foundation/search", response_model=Envelope)
    def foundation_search(payload: UnifiedSearchRequest, request: Request) -> Envelope:
        try:
            data = unified_search(system_from(request), payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if data["warnings"]:
            return warning("检索完成，存在降级或范围提示", data)
        return success("统一混合检索完成", data)

    @app.get("/api/foundation/overview", response_model=Envelope)
    def foundation_overview(request: Request) -> Envelope:
        try:
            data = FoundationService(system_from(request).settings.database_path).overview()
        except sqlite3.Error as exc:
            raise HTTPException(503, "资料台账读取超时或不可用，请重试") from exc
        return success("只读资料台账；未触发扫描", data)

    @app.get("/api/foundation/analytics", response_model=Envelope)
    def foundation_analytics(request: Request, days: int = Query(30, ge=7, le=90)) -> Envelope:
        try:
            data = FoundationService(system_from(request).settings.database_path).analytics(days)
        except sqlite3.Error as exc:
            raise HTTPException(503, "资料统计读取超时或不可用，请重试") from exc
        return success("已读取资料底座可视化统计；未触发扫描", data)

    @app.get("/api/foundation/included-files", response_model=Envelope)
    def foundation_included_files(
        request: Request,
        drive: str | None = Query(None, min_length=1, max_length=16),
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0, le=1000000),
    ) -> Envelope:
        settings = system_from(request).settings
        try:
            data = FoundationService(
                settings.database_path,
                settings.data_root / "machine-catalog" / "catalog.sqlite",
            ).included_files(drive=drive, limit=limit, offset=offset)
        except sqlite3.Error as exc:
            raise HTTPException(503, "纳入范围读取超时或不可用，请重试") from exc
        return success("已读取知识库实际纳入范围；未扫描原文件", data)

    @app.get("/api/foundation/documents", response_model=Envelope)
    def foundation_documents(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1000000),
    ) -> Envelope:
        try:
            service = FoundationService(system_from(request).settings.database_path)
            data = service.documents(limit, offset)
        except sqlite3.Error as exc:
            raise HTTPException(503, "文档对账读取超时或不可用，请重试") from exc
        return success("当前有效文件的全文与向量登记状态", data)

    @app.get("/api/foundation/files", response_model=Envelope)
    def foundation_files(
        request: Request,
        root_id: str,
        state: Literal["cataloged", "indexed", "skipped", "missing", "error"],
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=1000000),
    ) -> Envelope:
        try:
            data = FoundationService(system_from(request).settings.database_path).files(
                root_id=root_id, state=state, limit=limit, offset=offset
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, "文件状态读取超时或不可用，请重试") from exc
        return success("上次扫描状态；本次未读取原文件", data)

    @app.get("/api/sync/roots", response_model=Envelope)
    def sync_roots(request: Request) -> Envelope:
        items = system_from(request).sync.list_roots()
        return success(f"已读取 {len(items)} 个持续资料源", items)

    @app.post("/api/sync/roots/{root_id}/scan", response_model=Envelope)
    def scan_sync_root(root_id: str, request: Request) -> Envelope:
        try:
            result = system_from(request).sync.scan_root(root_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result.get("errors"):
            return warning("资料源刷新完成，部分正文未能索引", result)
        return success("资料源增量刷新完成", result)

    @app.post("/api/sync/catalog/search", response_model=Envelope)
    def search_source_catalog(payload: CatalogSearchRequest, request: Request) -> Envelope:
        items = system_from(request).sync.search_catalog(
            payload.query,
            root_id=payload.root_id,
            workspace_path=payload.workspace_path,
            limit=payload.limit,
        )
        return success(f"在资料地图中找到 {len(items)} 个文件", items)

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

    @agent_route("post", "/api/agent/context", response_model=Envelope)
    def agent_context(payload: AgentContextRequest, request: Request) -> Envelope:
        result = system_from(request).agent.prepare_context(
            task=payload.task,
            workspace_path=payload.workspace_path,
            domain=payload.domain,
            limit=payload.limit,
            include_restricted=payload.include_restricted,
        )
        return success(result["summary"], result)

    @agent_route("post", "/api/agent/run", response_model=Envelope)
    def run_agent(payload: AgentRunRequest, request: Request) -> Envelope:
        result = system_from(request).agent.run(
            task=payload.task,
            workspace_path=payload.workspace_path,
            domain=payload.domain,
            include_restricted=payload.include_restricted,
            persist_result=payload.persist_result,
            complexity=payload.complexity,
        )
        if result["status"] != "completed":
            return warning(
                "DeepSeek Agent 未完成任务",
                result,
                next_actions=[result["error"]["safe_retry"]],
            )
        return success(result["result"]["summary"], result)

    @agent_route("post", "/api/agent/closeout", response_model=Envelope)
    def closeout_agent(payload: AgentCloseoutRequest, request: Request) -> Envelope:
        result = system_from(request).agent.closeout_codex_turn(
            source_id=payload.source_id,
            workspace_path=payload.workspace_path,
        )
        if result["status"] != "completed":
            return warning(
                "Codex 任务收尾 Agent 未完成",
                result,
                next_actions=[result["error"]["safe_retry"]],
            )
        return success("已生成带证据状态的未审核知识候选", result)

    @agent_route("get", "/api/agent/runs/{run_id}/graph", response_model=Envelope)
    def agent_graph_status(run_id: str, request: Request) -> Envelope:
        try:
            result = system_from(request).agent.graph_status(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return success("已读取 LangGraph Agent 检查点状态", result)

    @agent_route("post", "/api/agent/runs/{run_id}/resume", response_model=Envelope)
    def resume_agent(run_id: str, request: Request) -> Envelope:
        result = system_from(request).agent.resume(run_id)
        if result["status"] != "completed":
            return warning(
                "LangGraph Agent 尚未恢复完成",
                result,
                next_actions=[result["error"]["safe_retry"]],
            )
        return success("LangGraph Agent 已从检查点恢复并完成", result)

    @agent_route("get", "/api/agent/jobs", response_model=Envelope)
    def agent_jobs(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Envelope:
        items = system_from(request).repository.list_agent_jobs(limit)
        return success(f"已读取 {len(items)} 个 Agent 后台任务", items)

    @agent_route("get", "/api/agent/usage", response_model=Envelope)
    def agent_usage(request: Request) -> Envelope:
        system = system_from(request)
        usage = system.repository.llm_usage_since(datetime.now(UTC).date().isoformat())
        usage["deepseek_configured"] = system.settings.deepseek_enabled
        usage["input_token_budget"] = system.settings.agent_daily_input_token_budget
        usage["output_token_budget"] = system.settings.agent_daily_output_token_budget
        return success("已读取今日 Agent Token 使用量", usage)

    @agent_route("get", "/api/agent/runs", response_model=Envelope)
    def agent_runs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Envelope:
        items = system_from(request).repository.list_agent_runs(limit)
        return success(f"已读取 {len(items)} 次智能体上下文运行记录", items)

    @app.get("/api/thread-journal/status", response_model=Envelope)
    def thread_journal_status(request: Request) -> Envelope:
        return success("已读取 Codex 线程日报状态", request.app.state.thread_journal.status())

    @app.post("/api/thread-journal/refresh", response_model=Envelope)
    def thread_journal_refresh(request: Request) -> Envelope:
        result = request.app.state.thread_journal.request_refresh()
        if result["status"] == "running":
            return success("会话摘要正在后台更新，本次未重复启动", result)
        return success("会话文件检查已进入后台，不会阻塞页面", result)

    @app.get("/api/persona", response_model=Envelope)
    def persona(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Envelope:
        items = system_from(request).repository.list_persona_observations(limit)
        return success(f"已读取 {len(items)} 条个人观察", items)

    @app.get("/api/personal-timeline", response_model=Envelope)
    def personal_timeline(
        request: Request,
        limit: int = Query(default=30, ge=1, le=366),
    ) -> Envelope:
        items = system_from(request).personal_timeline.list_days(limit)
        return success(f"已读取 {len(items)} 天个人活动", items)

    @app.get("/api/personal-timeline/status", response_model=Envelope)
    def personal_timeline_status(request: Request) -> Envelope:
        result = system_from(request).personal_timeline.readiness()
        return success("已读取个人时间线待整理状态；未调用模型", result)

    @app.get("/api/personal-timeline/{local_date}", response_model=Envelope)
    def personal_timeline_day(local_date: str, request: Request) -> Envelope:
        try:
            item = system_from(request).personal_timeline.get_day(local_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="这一天还没有整理记录")
        return success(f"已读取 {local_date} 的个人活动", item)

    @app.post("/api/personal-timeline/build", response_model=Envelope)
    def build_personal_timeline(
        payload: PersonalTimelineBuildRequest,
        request: Request,
    ) -> Envelope:
        try:
            item = system_from(request).personal_timeline.build_day(
                payload.local_date,
                provider=payload.provider,
                model=payload.model,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return success(
            f"已从本人聊天证据整理 {payload.local_date}",
            item,
            next_actions=["查看事实与推断是否准确；推断不会进入事实项"],
        )

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
            next_actions=["勾选需要进入知识库的微信会话，再执行只读结构检查"],
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
                "WeFlow XLSX 微信会话导入未完成",
                result,
                next_actions=[result["error"]["safe_retry"]],
            )
        imported = result["result"]["imported"]
        if result["status"] == "warning":
            failed_sessions = result["result"]["failed_sessions"]
            return warning(
                f"已导入 {imported} 条微信会话消息，{failed_sessions} 个会话需要重试",
                result,
                artifacts=result["artifacts"],
                next_actions=["查看失败会话并重试；已经成功导入的消息会自动去重。"],
            )
        return success(
            f"已从 WeFlow XLSX 导入 {imported} 条待归类微信会话消息",
            result,
            artifacts=result["artifacts"],
        )

    @app.get("/api/weflow/manual-sync/status", response_model=Envelope)
    def weflow_manual_sync_status(request: Request) -> Envelope:
        result = system_from(request).weflow_manual.status()
        return success("已读取微信手动同步状态", result)

    @app.post("/api/weflow/manual-sync/start", response_model=Envelope)
    def start_weflow_manual_sync(
        payload: WeFlowManualSyncRequest,
        request: Request,
    ) -> Envelope:
        try:
            result = system_from(request).weflow_manual.start(
                weflow_root=payload.weflow_root,
                records_path=payload.records_path,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result["status"] == "failed":
            return warning(
                "微信同步未能启动",
                result,
                next_actions=["确认 WeFlow 已退出且程序目录可用，然后重新点击同步。"],
            )
        return success(
            "微信同步已在无窗口后台启动",
            result,
            next_actions=["页面会自动更新导出和入库进度。"],
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
            f"已导入 {imported} 条 WeFlow 待归类微信会话消息",
            result,
            artifacts=result["artifacts"],
        )

    @app.get("/api/customers", response_model=Envelope)
    def customers(
        request: Request,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> Envelope:
        items = system_from(request).customers.list_customers(limit)
        return success(f"已读取 {len(items)} 个微信会话", items)

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
        try:
            result = system_from(request).customer_service.prepare_reply_context(
                customer_id=payload.customer_id,
                task=payload.task,
                recent_limit=payload.recent_limit,
                search_limit=payload.search_limit,
                include_restricted=payload.include_restricted,
            )
        except CustomerReviewRequired as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        customer = system_from(request).customers.get_customer(payload.customer_id)
        if customer and customer["review_status"] != "approved":
            raise HTTPException(
                status_code=409,
                detail="该微信会话尚未确认为业务客户，不能保存业务信号。",
            )
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
