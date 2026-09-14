import sqlite3

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.foundation import FoundationService, processing_state


def test_stage_does_not_invent_pending_or_success():
    assert processing_state("cataloged", None)[0] == "catalog_only"
    assert processing_state("skipped", "no_indexable_text")[0] == "parse_attention"
    assert processing_state("skipped", "unsupported_format")[0] == "excluded"
    assert processing_state("error", "PermissionError")[0] == "failed"
    assert processing_state("indexed", None)[0] == "index_recorded"


def test_readonly_connection_cannot_write(knowledge_system):
    service = FoundationService(knowledge_system.settings.database_path)
    with service.connect() as c, pytest.raises(sqlite3.OperationalError):
        c.execute("DELETE FROM sources")


def test_catalog_then_content_and_vector_gap(knowledge_system, source_root):
    (source_root / "business.md").write_text(
        "# Pump project\nFlow sensor requirements", encoding="utf-8"
    )
    sync = knowledge_system.sync
    root = sync.register_root(
        name="Fixture",
        root_path=str(source_root),
        connector_type="local_files",
        sync_mode="catalog",
    )
    sync.scan_root(root["id"])
    service = FoundationService(knowledge_system.settings.database_path)
    assert service.overview()["catalog_states"]["cataloged"] == 1
    assert service.documents()["total"] == 0
    page = service.files(root_id=root["id"], state="cataloged", limit=1)
    assert page["items"][0]["stage"] == "catalog_only"
    assert not page["has_more"]
    sync.register_root(
        name="Fixture", root_path=str(source_root), connector_type="local_files", sync_mode="index"
    )
    sync.scan_root(root["id"])
    doc = service.documents()["items"][0]
    assert doc["fulltext"] == "indexed"
    assert doc["vector"] == "not_fully_recorded"
    assert doc["quality"] == "not_manually_verified"
    assert service.documents(offset=1)["items"] == []


def test_analytics_excludes_codex_turns_from_file_charts(knowledge_system, source_root):
    (source_root / "report.md").write_text("# Report\nVerified delivery notes", encoding="utf-8")
    (source_root / "turn.md").write_text("# User task\nBuild the report", encoding="utf-8")
    knowledge_system.ingestion.import_file(
        source_root / "report.md", domain="work", privacy="private"
    )
    knowledge_system.ingestion.import_file(
        source_root / "turn.md", domain="work", privacy="private"
    )
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE sources SET source_type='codex-turn' WHERE original_name='turn.md'"
        )
        connection.commit()
    result = FoundationService(knowledge_system.settings.database_path).analytics(7)
    assert result["searchable_documents"] == 1
    assert result["original_documents"] == 1
    assert result["derived_summaries"] == 0
    assert result["searchable_chunks"] > 0
    assert result["vector_chunks"] == 0
    assert result["vector_coverage_percent"] == 0
    assert result["database_bytes"] > 0
    assert sum(item["count"] for item in result["source_types"]) == 1
    assert result["codex_records"] == 1
    assert len(result["ingest_trend"]) == 7
    assert result["ingest_trend"][-1]["count"] == 1


def test_included_files_lists_real_sources_and_disk_share(knowledge_system, source_root):
    source = source_root / "included.md"
    source.write_text("# Included\nSearchable source", encoding="utf-8")
    knowledge_system.ingestion.import_file(source, domain="work", privacy="private")
    result = FoundationService(knowledge_system.settings.database_path).included_files()
    assert result["total"] == 1
    assert result["items"][0]["path"] == str(source)
    assert result["items"][0]["level"] == "fulltext"
    drive = result["items"][0]["drive"]
    disk = next(item for item in result["disks"] if item["key"] == drive)
    assert disk["included_files"] == 1
    assert disk["included_bytes"] > 0
    assert disk["disk_share_percent"] >= 0


def test_parse_failure_and_filters(knowledge_system, source_root):
    sync = knowledge_system.sync
    root = sync.register_root(
        name="Fixture", root_path=str(source_root), connector_type="local_files"
    )
    (source_root / "empty.txt").write_text("", encoding="utf-8")
    sync.register_root(
        name="Fixture", root_path=str(source_root), connector_type="local_files", sync_mode="index"
    )
    sync.scan_root(root["id"])
    service = FoundationService(knowledge_system.settings.database_path)
    assert (
        service.files(root_id=root["id"], state="skipped")["items"][0]["stage"] == "parse_attention"
    )
    with pytest.raises(ValueError):
        service.files(root_id="bad", state="error")


def test_api_readonly_no_scan_or_model(test_settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not scan or embed")

    from pkas.sync import SyncService

    monkeypatch.setattr(SyncService, "scan_root", forbidden)
    with TestClient(create_app(test_settings)) as client:
        for endpoint in ["overview", "analytics", "included-files", "documents", "scopes"]:
            r = client.get(f"/api/foundation/{endpoint}")
            assert r.status_code == 200
        d = client.get("/api/foundation/scopes").json()["data"]
        assert d["authorized"] is False and d["scan_started"] is False
        assert client.get("/api/foundation/documents?limit=999").status_code == 422
        assert client.get("/api/foundation/files?root_id=bad&state=error").status_code == 400
        assert client.get("/api/foundation/files?root_id=bad&state=unknown").status_code == 422
