from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock
from time import monotonic, sleep
from typing import Protocol

import httpx

from pkas.config import Settings
from pkas.local_secrets import LocalSecretError, load_user_secret


class EmbeddingError(RuntimeError):
    """A safe embedding failure that never includes credentials or source text."""


_RECOVERABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


class EmbeddingProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(slots=True)
class SiliconFlowEmbeddingProvider:
    settings: Settings
    provider_name: str = "siliconflow"
    _http_client: httpx.Client | None = field(default=None, init=False, repr=False)
    _http_client_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _cache: OrderedDict[str, tuple[float, list[float]]] = field(
        default_factory=OrderedDict,
        init=False,
        repr=False,
    )
    _cache_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _cache_ttl_seconds: float = field(default=600.0, init=False, repr=False)
    _cache_max_entries: int = field(default=256, init=False, repr=False)

    @property
    def model_name(self) -> str:
        return self.settings.embedding_model

    @property
    def enabled(self) -> bool:
        return bool(self.settings.embedding_enabled and self._api_key())

    def _api_key(self) -> str | None:
        if self.settings.embedding_api_key:
            value = self.settings.embedding_api_key.get_secret_value().strip()
            if value:
                return value
        try:
            return load_user_secret(self.settings, "embedding_api_key")
        except LocalSecretError:
            return None

    def _get_http_client(self) -> httpx.Client:
        # A provider instance is reused by the MCP/API process.  Keeping one
        # httpx client avoids rebuilding the TLS connection for every query.
        with self._http_client_lock:
            if self._http_client is None:
                self._http_client = httpx.Client()
            return self._http_client

    def _close_http_client(self) -> None:
        with self._http_client_lock:
            if self._http_client is not None:
                self._http_client.close()
                self._http_client = None

    @staticmethod
    def _cache_key(text: str) -> str:
        # Store only a digest, never the user's query text, in the in-memory
        # cache metadata.
        return sha256(text.encode("utf-8")).hexdigest()

    def _cached_vector(self, text: str) -> list[float] | None:
        now = monotonic()
        key = self._cache_key(text)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            created_at, vector = cached
            if now - created_at > self._cache_ttl_seconds:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return list(vector)

    def _remember_vector(self, text: str, vector: list[float]) -> None:
        with self._cache_lock:
            key = self._cache_key(text)
            self._cache[key] = (monotonic(), list(vector))
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)

    def _post_json(
        self,
        endpoint: str,
        *,
        api_key: str,
        payload: dict[str, object],
        timeout: float,
    ) -> httpx.Response:
        try:
            return self._get_http_client().post(
                endpoint,
                json=payload,
                headers={"Authorization": "Bearer " + api_key},
                timeout=timeout,
            )
        except httpx.HTTPError:
            # A broken keep-alive connection must not poison later requests.
            self._close_http_client()
            raise

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        clean = [text.strip() for text in texts]
        if not clean or any(not text for text in clean):
            raise EmbeddingError("Embedding 输入不能为空。")
        api_key = self._api_key()
        if not self.enabled or not api_key:
            raise EmbeddingError("云端 Embedding 尚未在本机启用或配置。")

        single_query = len(clean) == 1
        if single_query:
            cached = self._cached_vector(clean[0])
            if cached is not None:
                return [cached]

        endpoint = f"{self.settings.embedding_base_url.rstrip('/')}/embeddings"
        payload = {"model": self.model_name, "input": clean, "encoding_format": "float"}
        attempts = max(1, int(self.settings.embedding_max_attempts))
        for attempt in range(attempts):
            try:
                response = self._post_json(
                    endpoint,
                    api_key=api_key,
                    payload=payload,
                    timeout=self.settings.embedding_timeout_seconds,
                )
            except httpx.HTTPError:
                if attempt + 1 == attempts:
                    raise EmbeddingError("Embedding 服务连接失败。") from None
                retry_after = None
            except ValueError:
                raise EmbeddingError("Embedding 服务响应解析失败。") from None
            except EmbeddingError:
                raise
            except (TimeoutError, OSError):
                if attempt + 1 == attempts:
                    raise EmbeddingError("Embedding 服务连接失败。") from None
                retry_after = None
            else:
                if response.status_code in _RECOVERABLE_HTTP_STATUS:
                    if attempt + 1 == attempts:
                        raise EmbeddingError(
                            f"Embedding 服务返回 HTTP {response.status_code}。"
                        )
                    raw_retry_after = response.headers.get("Retry-After")
                    try:
                        retry_after = float(raw_retry_after) if raw_retry_after else None
                    except (TypeError, ValueError):
                        retry_after = None
                elif not 200 <= response.status_code < 300:
                    raise EmbeddingError(
                        f"Embedding 服务返回 HTTP {response.status_code}。"
                    )
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        raise EmbeddingError("Embedding 服务响应解析失败。") from None
                    break
            # Respect a provider Retry-After hint when present; otherwise use a
            # bounded exponential backoff.  This keeps a transient network or
            # provider failure from hammering the endpoint.
            delay = retry_after or (
                float(self.settings.embedding_retry_base_seconds) * (2**attempt)
            )
            sleep(
                min(
                    max(0.0, delay),
                    float(self.settings.embedding_retry_max_seconds),
                )
            )

        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(clean):
            raise EmbeddingError("Embedding 服务返回的向量数量不匹配。")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in ordered]
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise EmbeddingError("Embedding 服务返回了无效向量。")
        result = [[float(value) for value in vector] for vector in vectors]
        if single_query:
            self._remember_vector(clean[0], result[0])
        return result
