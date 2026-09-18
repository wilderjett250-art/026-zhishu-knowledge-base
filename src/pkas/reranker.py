from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol

import httpx

from pkas.config import Settings
from pkas.local_secrets import LocalSecretError, load_user_secret


class RerankError(RuntimeError):
    """Safe rerank failure that never includes credentials or candidate text."""


@dataclass(slots=True)
class RerankResponse:
    scores: list[tuple[int, float]]
    usage: dict[str, Any]


class Reranker(Protocol):
    model_name: str

    @property
    def enabled(self) -> bool: ...

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> RerankResponse: ...


@dataclass(slots=True)
class SiliconFlowReranker:
    settings: Settings
    _http_client: httpx.Client | None = field(default=None, init=False, repr=False)
    _http_client_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    @property
    def model_name(self) -> str:
        return self.settings.rerank_model

    def _api_key(self) -> str | None:
        if self.settings.embedding_api_key:
            value = self.settings.embedding_api_key.get_secret_value().strip()
            if value:
                return value
        try:
            return load_user_secret(self.settings, "embedding_api_key")
        except LocalSecretError:
            return None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.rerank_enabled and self._api_key())

    def _get_http_client(self) -> httpx.Client:
        with self._http_client_lock:
            if self._http_client is None:
                self._http_client = httpx.Client()
            return self._http_client

    def _close_http_client(self) -> None:
        with self._http_client_lock:
            if self._http_client is not None:
                self._http_client.close()
                self._http_client = None

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> RerankResponse:
        clean_query = query.strip()
        clean_documents = [document.strip() for document in documents]
        if not clean_query or not clean_documents or any(not item for item in clean_documents):
            raise RerankError("Rerank 输入不能为空。")
        api_key = self._api_key()
        if not self.enabled or not api_key:
            raise RerankError("云端 Rerank 尚未在本机启用或配置。")
        endpoint = f"{self.settings.embedding_base_url.rstrip('/')}/rerank"
        payload = {
            "model": self.model_name,
            "query": clean_query,
            "documents": clean_documents,
            "top_n": max(1, min(top_n, len(clean_documents))),
            "return_documents": False,
        }
        try:
            response = self._get_http_client().post(
                endpoint,
                json=payload,
                headers={"Authorization": "Bearer " + api_key},
                timeout=self.settings.rerank_timeout_seconds,
            )
            if not 200 <= response.status_code < 300:
                raise RerankError(f"Rerank 服务返回 HTTP {response.status_code}。")
            response_payload = response.json()
        except httpx.HTTPError:
            self._close_http_client()
            raise RerankError("Rerank 服务连接或响应解析失败。") from None
        except (TimeoutError, OSError, ValueError):
            raise RerankError("Rerank 服务连接或响应解析失败。") from None
        results = response_payload.get("results")
        if not isinstance(results, list):
            raise RerankError("Rerank 服务返回了无效结果。")
        scores: list[tuple[int, float]] = []
        for item in results:
            try:
                index = int(item["index"])
                score = float(item["relevance_score"])
            except (KeyError, TypeError, ValueError):
                raise RerankError("Rerank 服务返回了无效排序项。") from None
            if index < 0 or index >= len(clean_documents):
                raise RerankError("Rerank 服务返回了越界排序项。")
            scores.append((index, score))
        return RerankResponse(scores=scores, usage=dict(response_payload.get("meta") or {}))
