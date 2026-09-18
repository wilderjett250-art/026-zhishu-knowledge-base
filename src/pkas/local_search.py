"""Explicit local-only search: no model, vector client, migrations or writes."""

import sqlite3
import time
from typing import Literal

from pydantic import BaseModel, Field

from pkas.customer_repository import CustomerRepository
from pkas.foundation import FoundationService
from pkas.repository import Repository


class LocalSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    scopes: list[Literal["files", "chats"]] = Field(
        default_factory=lambda: ["files"], min_length=1, max_length=2
    )
    include_restricted: bool = False
    limit: int = Field(default=10, ge=1, le=20)


class LocalSearchService:
    def __init__(self, database_path):
        # Repository constructors initialize/migrate the database. Read-only views do not.
        readonly = FoundationService(database_path)
        self.repository = Repository.__new__(Repository)
        self.repository.database = readonly
        self.customers = CustomerRepository.__new__(CustomerRepository)
        self.customers.database = readonly

    def search(self, payload: LocalSearchRequest) -> dict:
        started = time.monotonic()
        groups = {}
        warnings = []
        if not payload.query.strip():
            raise ValueError("请输入有效关键词")
        for scope in dict.fromkeys(payload.scopes):
            try:
                if scope == "files":
                    items = self.repository.search(
                        payload.query,
                        limit=payload.limit,
                        include_restricted=payload.include_restricted,
                        include_task_records=False,
                    )
                else:
                    items = self.customers.search_messages(
                        payload.query,
                        limit=payload.limit,
                        include_restricted=payload.include_restricted,
                        include_candidates=True,
                    )
                for item in items:
                    item["snippet"] = str(item.get("snippet") or "")[:800]
                groups[scope] = {"status": "ok", "results": items}
            except sqlite3.Error:
                groups[scope] = {"status": "error", "results": []}
                warnings.append(f"{scope}：本地索引读取失败或超时，请缩小关键词后重试")
        if "chats" in payload.scopes and not payload.include_restricted:
            warnings.append(
                "未包含受限记录；已导入的微信记录可能因此被过滤。可明确勾选仅本机搜索受限资料。"
            )
        return {
            "mode": "local_fulltext",
            "cloud_called": False,
            "task_records_included": False,
            "scopes": payload.scopes,
            "include_restricted": payload.include_restricted,
            "groups": groups,
            "warnings": warnings,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "note": "仅搜索已建内容索引的资料；不含仅登记文件。结果是来源证据，不是完成事实。",
        }
