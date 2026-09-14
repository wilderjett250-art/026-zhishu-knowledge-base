"""Shared EXE and MCP contract; adapters never implement ranking."""

import sqlite3
import time
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field


class UnifiedSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    domain: Literal["work", "self", "shared", "distill"] | None = None
    scopes: list[Literal["files", "chats"]] = Field(
        default_factory=lambda: ["files"], min_length=1, max_length=2
    )
    limit: int = Field(default=10, ge=1, le=50)
    include_restricted: bool = False
    rerank_mode: Literal["auto", "never", "always"] = "auto"
    expand_parent: bool = True
    retrieval_mode: Literal["a", "b", "ab"] = "ab"


def unified_search(system, request: UnifiedSearchRequest) -> dict:
    if not request.query.strip():
        raise ValueError("请输入有效关键词")
    started = time.monotonic()
    groups, warnings, results = {}, [], []
    mode, health = "chat_fts_only", None
    if "files" in request.scopes:
        response = system.retrieval.search(
            request.query,
            domain=request.domain,
            limit=request.limit,
            include_restricted=request.include_restricted,
            rerank_mode=request.rerank_mode,
            expand_parent=request.expand_parent,
            include_task_records=False,
            retrieval_mode=request.retrieval_mode,
        )
        mode = response.mode
        health = asdict(response.health) if response.health else None
        warnings.extend(response.warnings)
        if request.retrieval_mode in {"b", "ab"} and not (
            health and health.get("channels", {}).get("vector")
        ):
            warnings.append(
                "本次没有向量命中：B库未启用、不可用或没有找到候选。"
            )
        groups["files"] = {"status": "ok", "results": response.results, "mode": mode}
        results.extend(response.results)
    if "chats" in request.scopes:
        warnings.append("聊天尚无向量索引，本次仅全文检索；聊天原文不会发送到云端。")
        if request.domain:
            groups["chats"] = {"status": "error", "results": []}
            warnings.append("聊天暂不支持领域过滤，未执行聊天查询；请清空领域后重试。")
        else:
            try:
                chats = system.customers.search_messages(
                    request.query,
                    limit=request.limit,
                    include_candidates=True,
                    include_restricted=request.include_restricted,
                )
                groups["chats"] = {"status": "ok", "results": chats, "mode": "fts_only"}
                results.extend(chats)
            except sqlite3.Error:
                groups["chats"] = {"status": "error", "results": []}
                warnings.append("聊天索引读取失败，不能视为没有资料。")
        if not request.include_restricted:
            warnings.append("未包含受限聊天，需要时请明确选择本机可见范围。")
    return {
        "contract": "pkas.search.v1",
        "mode": mode,
        "health": health,
        "results": results,
        "groups": groups,
        "warnings": list(dict.fromkeys(warnings)),
        "parameters": request.model_dump(),
        "scopes": request.scopes,
        "include_restricted": request.include_restricted,
        "task_records_included": False,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "note": "A=目录/全文精确检索，B=正文向量检索，AB=两者融合；与MCP共用入口。",
    }
