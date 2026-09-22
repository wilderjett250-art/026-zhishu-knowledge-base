"""HTTP contract for the localhost-only RPA bridge."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pkas.rpa_bridge import (
    RPA_API_PREFIX,
    RpaBridgeDisabled,
    RpaBridgePolicy,
    RpaBridgeRateLimited,
    RpaBridgeService,
    RpaBridgeUnauthorized,
    RpaBridgeUnavailable,
)
from pkas.search_gateway import UnifiedSearchRequest, unified_search

router = APIRouter(prefix=RPA_API_PREFIX, tags=["RPA localhost bridge"])
RpaScope = Literal["files", "chats"]


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RpaMessagePayload(_StrictPayload):
    conversation_key: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=8_000)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=240)
    sender_key: str | None = Field(default=None, min_length=1, max_length=240)
    message_at: str | None = Field(default=None, min_length=1, max_length=80)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("conversation_key", "text", "source_event_id", "sender_key", "message_at")
    @classmethod
    def reject_control_characters(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("字段不能包含空字符。")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 20:
            raise ValueError("metadata 最多允许 20 项。")
        clean: dict[str, str] = {}
        for key, item in value.items():
            if not key or len(key) > 64 or len(item) > 400 or "\x00" in key or "\x00" in item:
                raise ValueError("metadata 字段不符合本机桥限制。")
            clean[key] = item
        return clean


class RpaSearchPayload(_StrictPayload):
    query: str = Field(min_length=1, max_length=2_000)
    domain: Literal["work", "self", "shared", "distill"] | None = None
    scopes: list[RpaScope] = Field(default_factory=lambda: ["files"])
    limit: int = Field(default=5, ge=1, le=8)

    @field_validator("query")
    @classmethod
    def reject_nul_query(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("query 不能包含空字符。")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(
        cls, value: list[RpaScope]
    ) -> list[RpaScope]:
        if not value or len(value) > 2:
            raise ValueError("scopes 必须包含 1 到 2 个范围。")
        return list(dict.fromkeys(value))


def bridge_from(request: Request) -> RpaBridgeService:
    bridge = getattr(request.app.state, "rpa_bridge", None)
    if not isinstance(bridge, RpaBridgeService):
        raise RuntimeError("RPA 本机桥尚未初始化。")
    return bridge


def authorize(
    request: Request,
    authorization: str | None,
) -> tuple[RpaBridgeService, RpaBridgePolicy]:
    bridge = bridge_from(request)
    try:
        policy = bridge.authorize(
            client_host=request.client.host if request.client else None,
            authorization=authorization,
            origin=request.headers.get("origin"),
        )
    except RpaBridgeDisabled as exc:
        raise HTTPException(status_code=404, detail="RPA 本机桥未启用。") from exc
    except RpaBridgeUnauthorized as exc:
        raise HTTPException(
            status_code=401,
            detail="RPA 本机桥认证失败。",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except RpaBridgeUnavailable as exc:
        raise HTTPException(status_code=503, detail="RPA 本机桥当前不可用。") from exc
    except RpaBridgeRateLimited as exc:
        raise HTTPException(status_code=429, detail="RPA 本机桥请求过快，请稍后重试。") from exc
    return bridge, policy


@router.get("/status")
def rpa_status(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    bridge, _ = authorize(request, authorization)
    return bridge.status()


@router.post("/messages")
def rpa_record_message(
    payload: RpaMessagePayload,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    bridge, _ = authorize(request, authorization)
    result = bridge.record_message(
        conversation_key=payload.conversation_key,
        text=payload.text,
        source_event_id=payload.source_event_id,
        sender_key=payload.sender_key,
        message_at=payload.message_at,
        metadata=payload.metadata,
    )
    return {"contract": "pkas.rpa-loopback.message.v1", **result}


@router.post("/search")
def rpa_search(
    payload: RpaSearchPayload,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    bridge, policy = authorize(request, authorization)
    requested_scopes: list[RpaScope] = list(payload.scopes)
    effective_scopes: list[RpaScope] = list(requested_scopes)
    warnings: list[str] = []
    if "chats" in effective_scopes and not policy.allow_restricted_context:
        effective_scopes = [scope for scope in effective_scopes if scope != "chats"]
        warnings.append("聊天资料未被授权给 RPA；本次只检索普通资料。")
    if not effective_scopes:
        effective_scopes = ["files"]
    try:
        response = unified_search(
            request.app.state.system,
            UnifiedSearchRequest(
                query=payload.query,
                domain=payload.domain,
                scopes=effective_scopes,
                limit=min(payload.limit, policy.max_results),
                include_restricted=policy.allow_restricted_context,
                rerank_mode="never",
                expand_parent=True,
                retrieval_mode=policy.retrieval_mode,
            ),
        )
    except (ValueError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="本机知识检索暂不可用。") from exc
    evidence = [bridge.compact_evidence(item) for item in response["results"][: policy.max_results]]
    bridge.audit_search(
        result_count=len(evidence),
        scopes=effective_scopes,
        retrieval_mode=policy.retrieval_mode,
    )
    warnings.extend(str(item) for item in response.get("warnings", [])[:5])
    return {
        "contract": "pkas.rpa-loopback.search.v1",
        "mode": response["mode"],
        "evidence": evidence,
        "result_count": len(evidence),
        "requested_scopes": requested_scopes,
        "effective_scopes": effective_scopes,
        "warnings": list(dict.fromkeys(warnings)),
        "evidence_policy": {
            "raw_database_access": False,
            "raw_file_path_returned": False,
            "restricted_context": policy.allow_restricted_context,
            "rerank": "disabled_for_rpa_bridge",
        },
    }
