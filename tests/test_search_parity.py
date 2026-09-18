import pytest
from fastapi.testclient import TestClient

import pkas.mcp_server as mcp
from pkas.api import create_app
from pkas.vector_index import VectorSearchResult


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("rerank_mode", ["auto", "never", "always"])
def test_exe_mcp_identical_contract(
    knowledge_system, test_settings, source_root, monkeypatch, semantic, rerank_mode
):
    p = source_root / "pump.md"
    p.write_text("Pump sensor precision requirements", encoding="utf-8")
    imported = knowledge_system.ingestion.import_file(p, domain="work", privacy="private")
    if semantic:
        with knowledge_system.database.connect() as connection:
            connection.execute(
                """UPDATE sources
                   SET metadata_json = json_set(
                       COALESCE(metadata_json, '{}'),
                       '$.requested_processing_level', 'L3'
                   )
                 WHERE id = ?""",
                (imported["source_id"],),
            )
            connection.commit()
    monkeypatch.setattr(mcp, "system", lambda: knowledge_system)

    def vector(self, query, **kwargs):
        items = knowledge_system.repository.search(query) if semantic else []
        for item in items:
            item["vector_score"] = 0.9
        return VectorSearchResult(items, "ok" if semantic else "disabled")

    from pkas.vector_index import QdrantVectorIndex

    monkeypatch.setattr(QdrantVectorIndex, "search", vector)
    if semantic:
        monkeypatch.setattr(QdrantVectorIndex, "enabled", property(lambda self: True))
        monkeypatch.setattr(
            QdrantVectorIndex,
            "coverage",
            lambda self: {"eligible": 1, "indexed": 1, "pending": 0, "coverage": 1.0},
        )
    with TestClient(create_app(test_settings)) as client:
        parameters = {"query": "sensor", "limit": 10, "rerank_mode": rerank_mode}
        api = client.post("/api/foundation/search", json=parameters).json()["data"]
        tool = mcp.search_knowledge(**parameters)["retrieval"]
        for value in (api, tool):
            value.pop("elapsed_ms")
        assert api == tool
        assert api["mode"] == ("hybrid" if semantic else "fts_only")
        assert api["results"]
        if not semantic:
            assert api["warnings"]
