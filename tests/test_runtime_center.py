from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.runtime_manager import RuntimeManager
from pkas.usage_metrics import UsageMetrics


def test_usage_counts_and_no_samples(tmp_path):
    metrics = UsageMetrics(tmp_path)
    assert metrics.summary()["success_rate"] is None
    metrics.record("mcp", "search_knowledge", "success", 0.01)
    metrics.record("mcp", "search_knowledge", "error", 0.03)
    result = metrics.summary()
    assert result["calls"] == 2
    assert result["success_rate"] == 0.5
    assert result["operations"][0]["average_ms"] == 20
    assert result["operations"][0]["failures"] == 1


def test_runtime_does_not_adopt_external_process(test_settings, monkeypatch):
    manager = RuntimeManager(test_settings)
    monkeypatch.setattr(manager, "_qdrant_ready", lambda: True)
    with pytest.raises(ValueError):
        manager.control("qdrant", "start")
    with pytest.raises(ValueError):
        manager.control("qdrant", "stop")
    assert manager.children == {}
    with pytest.raises(ValueError):
        manager.control("indexer", "start", False)


def test_ensure_qdrant_accepts_an_already_ready_external_service(test_settings, monkeypatch):
    manager = RuntimeManager(test_settings)
    monkeypatch.setattr(manager, "_qdrant_ready", lambda: True)
    result = manager.ensure_qdrant_ready()
    qdrant = next(item for item in result["services"] if item["id"] == "qdrant")
    assert qdrant["status"] == "external"
    assert manager.children == {}


def test_api_usage_and_confirmation_gate(test_settings):
    with TestClient(create_app(test_settings)) as client:
        assert client.get("/api/runtime/ping").json()["service"] == "pkas-runtime"
        initial = client.get("/api/runtime/overview").json()["data"]
        assert initial["usage"]["calls"] == 0
        assert client.get("/api/workflows").status_code == 200
        result = client.get("/api/runtime/overview").json()["data"]
        assert result["usage"]["calls"] == 1
        assert result["usage"]["success_rate"] == 1
        assert (
            client.post("/api/runtime/services/indexer", json={"action": "start"}).status_code
            == 422
        )
        assert (
            client.post(
                "/api/runtime/services/indexer", json={"action": "start", "confirmed": True}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/runtime/services/qdrant",
                json={"action": "start", "confirmed": True},
                headers={"Origin": "https://other.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/runtime/services/unknown", json={"action": "start", "confirmed": True}
            ).status_code
            == 409
        )


def test_runtime_overview_separates_actionable_vector_work(test_settings, tmp_path):
        with TestClient(create_app(test_settings)) as client:
            system = client.app.state.system
            system.settings.embedding_scope = "all_formal"
            source = tmp_path / "formal.md"
        source.write_text("正式资料", encoding="utf-8")
        imported = system.ingestion.import_file(source, domain="work", privacy="private")
        with system.database.connect() as connection:
            chunk_id = connection.execute(
                "SELECT id FROM chunks WHERE source_id=? LIMIT 1", (imported["source_id"],)
            ).fetchone()["id"]
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """INSERT INTO index_outbox(event_key,operation,entity_type,entity_id,status,
                attempts,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (uuid4().hex, "upsert", "chunk", chunk_id, "pending", 0, now, now, now),
            )
            connection.execute(
                """INSERT INTO index_outbox(event_key,operation,entity_type,entity_id,status,
                attempts,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    uuid4().hex,
                    "reconcile",
                    "source",
                    imported["source_id"],
                    "pending",
                    0,
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()

        queues = client.get("/api/runtime/overview").json()["data"]["queues"]
        assert queues["index_outbox_actionable"]["pending"] >= 1
        assert queues["index_outbox_background"]["pending"] >= 1
