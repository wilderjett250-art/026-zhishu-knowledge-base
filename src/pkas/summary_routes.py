"""Local UI and external-agent protocol for resumable summary jobs."""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from pkas.schemas import Envelope
from pkas.summary_agent import AgentError, CodexAgent
from pkas.summary_jobs import SummaryEdit, SummaryExport, SummaryJobRequest, SummaryPromotion


def local_only(request: Request):
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "整理接口仅供本机调用")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "不接受外部网页发起的整理请求")


router = APIRouter(prefix="/api/foundation/summary-jobs", dependencies=[Depends(local_only)])


def result(data):
    return Envelope(status="success", summary="已读取或更新整理任务", data=data)


def service(request):
    return request.app.state.summary_jobs


class ProviderProbe(BaseModel):
    executable: str = ""


class AgentItem(BaseModel):
    id: str
    summary: str = Field(max_length=2000)
    category_id: str
    evidence: str = Field(max_length=2000)
    uncertainty: str = Field(max_length=2000)


class AgentDelivery(BaseModel):
    packet_id: str
    items: list[AgentItem] = Field(max_length=20)


@router.get("")
def recent(request: Request):
    return result(service(request).recent())


@router.get("/catalog-progress")
def catalog_progress(request: Request):
    try:
        return result(service(request).catalog_progress())
    except (ValueError, OSError):
        raise HTTPException(409, "无法读取A库分类台账") from None


@router.post("/catalog-progress/retry")
def retry_catalog_progress(request: Request):
    try:
        return result(service(request).retry_catalog_progress())
    except (ValueError, OSError):
        raise HTTPException(409, "无法重新计算A库分类范围") from None


@router.get("/catalog-preflight")
def catalog_preflight_status(request: Request):
    return result(service(request).catalog_preflight_status())


@router.get("/catalog-preflight/categories")
def catalog_preflight_categories(request: Request):
    try:
        return result(service(request).catalog_preflight_categories())
    except (ValueError, OSError):
        raise HTTPException(409, "无法读取本地暂定分类") from None


@router.get("/catalog-preflight/reviews")
def catalog_preflight_reviews(
    request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)
):
    try:
        return result(service(request).catalog_preflight_reviews(offset=offset, limit=limit))
    except (ValueError, OSError):
        raise HTTPException(409, "无法读取本地待复查明细") from None


@router.get("/catalog-preflight/files")
def catalog_preflight_files(
    request: Request,
    category_id: str = Query(..., min_length=1, max_length=64),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
):
    try:
        return result(service(request).catalog_preflight_files(
            category_id, offset=offset, limit=limit
        ))
    except (ValueError, OSError):
        raise HTTPException(409, "无法读取当前分类的文件明细") from None


@router.post("/catalog-preflight/start")
def start_catalog_preflight(request: Request):
    try:
        return result(service(request).start_catalog_preflight())
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(
            409, str(exc) if isinstance(exc, ValueError) else "无法启动本地逐文件轻读"
        ) from None


@router.post("/catalog-preflight/pause")
def pause_catalog_preflight(request: Request):
    return result(service(request).pause_catalog_preflight())


@router.get("/catalog-classifications")
def catalog_classifications(request: Request):
    try:
        return result({"counts": service(request).catalog_classification_counts()})
    except (ValueError, OSError):
        raise HTTPException(409, "无法读取A库分类结果") from None


@router.post("/catalog-promote-preview")
def catalog_promote_preview(payload: SummaryPromotion, request: Request):
    try:
        selection = service(request).catalog_promotion_selection(payload)
        return result(request.app.state.intake.preview_classified(selection))
    except (ValueError, OSError) as exc:
        raise HTTPException(
            409, str(exc) if isinstance(exc, ValueError) else "无法生成分类台账入库预览"
        ) from None


@router.post("/probe")
def probe(payload: ProviderProbe, request: Request):
    try:
        with CodexAgent(
            Path(request.app.state.system.settings.project_root), executable=payload.executable
        ) as agent:
            return result({"models": agent.models(), "inference_called": False})
    except (AgentError, OSError):
        raise HTTPException(409, "Codex连接失败，请检查程序路径、登录或版本") from None


@router.post("")
def create(payload: SummaryJobRequest, request: Request):
    try:
        return result(service(request).create(payload))
    except (ValueError, OSError) as exc:
        raise HTTPException(
            409, str(exc) if isinstance(exc, ValueError) else "无法创建任务"
        ) from None


@router.get("/{job}")
def read(
    job: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=0, le=100)
):
    try:
        return result(service(request).view(job, offset, limit))
    except (ValueError, OSError):
        raise HTTPException(404, "任务不存在") from None


@router.post("/{job}/resume")
def resume(job: str, request: Request):
    try:
        service(request).resume(job)
        return result(service(request).view(job))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, ValueError) else "无法续扫") from None


@router.post("/{job}/pause")
def pause(job: str, request: Request):
    service(request).pause(job)
    return result({"message": "暂停已请求，当前本地文件处理结束后保存进度"})


@router.post("/{job}/retry-errors")
def retry_errors(job: str, request: Request):
    try:
        service(request).retry_errors(job)
        return result(service(request).view(job))
    except (ValueError, RuntimeError, OSError):
        raise HTTPException(409, "无法重试，请先暂停运行中的任务") from None


@router.post("/{job}/files/{ident}")
def edit(job: str, ident: int, payload: SummaryEdit, request: Request):
    try:
        service(request).edit(job, ident, payload)
        return result(service(request).view(job))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, ValueError) else "修改失败") from None


@router.post("/{job}/export")
def export(job: str, payload: SummaryExport, request: Request):
    try:
        return result(service(request).export(job, payload.destination))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc) if isinstance(exc, ValueError) else "导出失败") from None


@router.post("/{job}/promote-preview")
def promote_preview(job: str, payload: SummaryPromotion, request: Request):
    try:
        selection = service(request).promotion_selection(job, payload)
        return result(request.app.state.intake.preview_classified(selection))
    except (ValueError, OSError) as exc:
        raise HTTPException(
            409, str(exc) if isinstance(exc, ValueError) else "无法生成处理预览"
        ) from None


@router.get("/{job}/agent-packet")
def packet(job: str, request: Request):
    try:
        return result(service(request).packet(job))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/{job}/agent-result")
def deliver(job: str, payload: AgentDelivery, request: Request):
    try:
        jobs = service(request)
        if jobs.view(job, limit=0)["request"]["provider"] != "external":
            raise ValueError("此任务没有选择外部Agent")
        jobs.accept(job, payload.packet_id, {"items": [i.model_dump() for i in payload.items]})
        return result(
            {"accepted": True, "next": "继续读取agent-packet，返回null后调用resume生成MD"}
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
