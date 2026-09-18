import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.catalog_import import import_catalog_items
from pkas.local_search import LocalSearchRequest, LocalSearchService


def test_selected_catalog_file_becomes_searchable(knowledge_system, source_root):
    path = source_root / "requirements.md"
    path.write_text("# Irrigation\nPump sensor precision requirement", encoding="utf-8")
    root = knowledge_system.sync.register_root(
        name="Fixture",
        root_path=str(source_root),
        connector_type="local_files",
        sync_mode="catalog",
    )
    knowledge_system.sync.scan_root(root["id"])
    s = LocalSearchService(knowledge_system.settings.database_path)
    p = LocalSearchRequest(query="precision")
    assert not s.search(p)["groups"]["files"]["results"]
    with knowledge_system.database.connect() as c:
        item = c.execute("SELECT id FROM sync_items WHERE root_id=?", (root["id"],)).fetchone()[0]
    with pytest.raises(ValueError):
        import_catalog_items(knowledge_system.settings, [item], confirmed=False)
    first = import_catalog_items(knowledge_system.settings, [item], confirmed=True)
    assert first[0]["status"] == "imported" and first[0]["original_unchanged"]
    assert len(s.search(p)["groups"]["files"]["results"]) == 1
    second = import_catalog_items(knowledge_system.settings, [item], confirmed=True)
    assert second[0]["status"] == "duplicate"
    assert path.read_text(encoding="utf-8").endswith("precision requirement")


def test_task_records_not_in_local_files(knowledge_system):
    knowledge_system.ingestion.import_text(
        text="# User request\nUniqueTaskProbe",
        title="UniqueTaskProbe",
        original_uri="codex://test",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata={"record_kind": "user_task", "assistant_output_indexed": False},
    )
    result = LocalSearchService(knowledge_system.settings.database_path).search(
        LocalSearchRequest(query="UniqueTaskProbe")
    )
    assert result["groups"]["files"]["results"] == []


def test_local_search_partial_failure(knowledge_system, monkeypatch):
    service = LocalSearchService(knowledge_system.settings.database_path)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(service.repository, "search", fail)
    result = service.search(LocalSearchRequest(query="test", scopes=["files", "chats"]))
    assert result["groups"]["files"]["status"] == "error"
    assert result["groups"]["chats"]["status"] == "ok"


def test_api_local_chat_permission(test_settings, knowledge_system, monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"
    inspected = knowledge_system.weflow.inspect_chatlab_file(str(fixture))
    knowledge_system.weflow.import_chatlab_file(
        path=str(fixture), privacy="restricted", inspection_token=inspected["inspection_token"]
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("local search must not call model or vector")

    from pkas.vector_index import QdrantVectorIndex

    monkeypatch.setattr(QdrantVectorIndex, "search", forbidden)
    with TestClient(create_app(test_settings)) as client:
        query = {"query": "报价", "scopes": ["chats"]}
        filtered = client.post("/api/foundation/search", json=query).json()
        assert filtered["data"]["groups"]["chats"]["results"] == []
        assert filtered["data"]["warnings"]
        query["include_restricted"] = True
        found = client.post("/api/foundation/search", json=query).json()["data"]
        assert found["mode"] == "chat_fts_only"
        assert found["groups"]["chats"]["results"]
        assert client.post("/api/foundation/search", json={"query": "   "}).status_code == 400
        assert (
            client.post("/api/foundation/search", json={"query": "x", "scopes": []}).status_code
            == 422
        )
