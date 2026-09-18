from pydantic import SecretStr

from pkas.embeddings import SiliconFlowEmbeddingProvider


class _Response:
    def __init__(self, status_code: int, payload: dict, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self) -> dict:
        return self.payload


def test_embedding_retries_a_transient_rate_limit_without_replaying_callers(
    test_settings, monkeypatch
) -> None:
    test_settings.embedding_api_key = SecretStr("test-only-embedding-key")
    provider = SiliconFlowEmbeddingProvider(test_settings)
    attempts = []

    def fake_post(self, endpoint, *, api_key, payload, timeout):
        _ = (self, endpoint, api_key, payload, timeout)
        attempts.append(True)
        if len(attempts) == 1:
            return _Response(429, {}, {})
        return _Response(200, {"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(SiliconFlowEmbeddingProvider, "_post_json", fake_post)
    monkeypatch.setattr("pkas.embeddings.sleep", lambda seconds: None)

    assert provider.embed(["retry-safe health probe"]) == [[0.1, 0.2]]
    assert len(attempts) == 2


def test_embedding_uses_retry_after_header(test_settings, monkeypatch) -> None:
    test_settings.embedding_api_key = SecretStr("test-only-embedding-key")
    test_settings.embedding_max_attempts = 2
    provider = SiliconFlowEmbeddingProvider(test_settings)
    delays = []
    attempts = []

    def fake_post(self, endpoint, *, api_key, payload, timeout):
        _ = (self, endpoint, api_key, payload, timeout)
        attempts.append(True)
        if len(attempts) == 1:
            return _Response(429, {}, {"Retry-After": "7"})
        return _Response(200, {"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(SiliconFlowEmbeddingProvider, "_post_json", fake_post)
    monkeypatch.setattr("pkas.embeddings.sleep", delays.append)

    assert provider.embed(["retry-after health probe"]) == [[0.1, 0.2]]
    assert delays == [7.0]


def test_single_embedding_query_uses_bounded_in_memory_cache(test_settings, monkeypatch) -> None:
    test_settings.embedding_api_key = SecretStr("test-only-embedding-key")
    provider = SiliconFlowEmbeddingProvider(test_settings)
    calls = []

    def fake_post(self, endpoint, *, api_key, payload, timeout):
        _ = (self, endpoint, api_key, payload, timeout)
        calls.append(True)
        return _Response(200, {"data": [{"index": 0, "embedding": [0.3, 0.4]}]})

    monkeypatch.setattr(SiliconFlowEmbeddingProvider, "_post_json", fake_post)

    assert provider.embed(["same query"]) == [[0.3, 0.4]]
    assert provider.embed(["same query"]) == [[0.3, 0.4]]
    assert len(calls) == 1
