import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pkas.config import Settings
from pkas.local_secrets import LocalSecretError, load_user_secret


class EmbeddingError(RuntimeError):
    """A safe embedding failure that never includes credentials or source text."""


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str

    @property
    def enabled(self) -> bool: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(slots=True)
class SiliconFlowEmbeddingProvider:
    settings: Settings
    provider_name: str = "siliconflow"

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

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        clean = [text.strip() for text in texts]
        if not clean or any(not text for text in clean):
            raise EmbeddingError("Embedding 输入不能为空。")
        api_key = self._api_key()
        if not self.enabled or not api_key:
            raise EmbeddingError("云端 Embedding 尚未在本机启用或配置。")

        endpoint = f"{self.settings.embedding_base_url.rstrip('/')}/embeddings"
        payload = json.dumps(
            {"model": self.model_name, "input": clean, "encoding_format": "float"},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.settings.embedding_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise EmbeddingError(f"Embedding 服务返回 HTTP {exc.code}。") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise EmbeddingError("Embedding 服务连接或响应解析失败。") from None

        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(clean):
            raise EmbeddingError("Embedding 服务返回的向量数量不匹配。")
        ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
        vectors = [item.get("embedding") for item in ordered]
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise EmbeddingError("Embedding 服务返回了无效向量。")
        return [[float(value) for value in vector] for vector in vectors]
