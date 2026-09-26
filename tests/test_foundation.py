import json
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


def test_included_files_reuses_snapshot_until_database_changes(
    knowledge_system,
    monkeypatch,
):
    service = FoundationService(knowledge_system.settings.database_path)
    original = service._included_files_uncached
    calls = 0

    def counted(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(service, "_included_files_uncached", counted)
    service.included_files(drive="ALL", limit=1)
    service.included_files(drive="E", limit=1)
    assert calls == 1

    with knowledge_system.database.connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO app_meta(key,value) VALUES('snapshot-cache-test','1')"
        )
        connection.commit()
    service.included_files(drive="ALL", limit=1)
    assert calls == 2


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
    assert doc["fulltext"] == "recorded"
    assert doc["vector"] == "not_fully_recorded"
    assert doc["quality"] == "not_manually_verified"
    assert doc["parser_version"] == "hybrid-v2"
    assert doc["parsing"] == "current"
    assert service.documents()["fts_consistent"] is None
    verified = service.documents(verify_fulltext=True)
    assert verified["items"][0]["fulltext"] == "indexed"
    assert verified["fts_consistent"] is True
    assert service.documents(offset=1)["items"] == []


def test_documents_expose_safe_visual_attention_details(knowledge_system, source_root):
    source = source_root / "scan-notice.md"
    source.write_text("placeholder", encoding="utf-8")
    stored = knowledge_system.ingestion.import_file(
        source, domain="work", privacy="private"
    )
    metadata = {
        "extraction": {
            "initial_quality": {
                "reasons": ["low_text_or_scanned_pdf_pages"],
                "visual_pages": [4, 2, 4],
            }
        }
    }
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE sources SET metadata_json=? WHERE id=?",
            (json.dumps(metadata), stored["source_id"]),
        )
        connection.commit()

    item = FoundationService(knowledge_system.settings.database_path).documents()["items"][0]
    assert item["quality"] == "attention"
    assert item["quality_reasons"] == ["low_text_or_scanned_pdf_pages"]
    assert item["visual_pages"] == [2, 4]


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


def test_analytics_keeps_unreviewed_thread_summaries_separate(knowledge_system, source_root):
    source = source_root / "draft-summary.md"
    source.write_text("# Draft\nAuto-generated conversation recap", encoding="utf-8")
    knowledge_system.ingestion.import_file(source, domain="work", privacy="private")
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE sources SET source_type='thread-summary' WHERE original_name='draft-summary.md'"
        )
        connection.commit()

    result = FoundationService(knowledge_system.settings.database_path).analytics(7)

    assert result["searchable_documents"] == 0
    assert result["searchable_chunks"] == 0
    assert result["original_documents"] == 0
    assert result["derived_summaries"] == 1
    assert result["source_types"] == []


def test_included_files_lists_real_sources_and_disk_share(knowledge_system, source_root):
    source = source_root / "included.md"
    source.write_text("# Included\nSearchable source", encoding="utf-8")
    knowledge_system.ingestion.import_file(source, domain="work", privacy="private")
    result = FoundationService(knowledge_system.settings.database_path).included_files()
    assert result["total"] == 1
    assert result["items"][0]["path"] == str(source)
    assert result["items"][0]["level"] == "fulltext"
    assert result["level_counts"] == {"fulltext": 1}
    assert result["canonical_level_counts"] == {"fulltext": 1}
    drive = result["items"][0]["drive"]
    disk = next(item for item in result["disks"] if item["key"] == drive)
    assert disk["included_files"] == 1
    assert disk["included_bytes"] > 0
    assert disk["disk_share_percent"] >= 0


def test_included_files_counts_deduplicated_original_paths(knowledge_system, source_root):
    source = source_root / "canonical.md"
    alias_path = source_root / "copy" / "same-content.md"
    alias_path.parent.mkdir()
    payload = "同内容资料只需要一份正文和切片。"
    source.write_text(payload, encoding="utf-8")
    alias_path.write_text(payload, encoding="utf-8")
    stored = knowledge_system.ingestion.import_file(source, domain="work", privacy="private")
    knowledge_system.repository.register_source_alias(
        source_id=stored["source_id"],
        original_uri=str(alias_path),
        original_name=alias_path.name,
        vault_path=str(alias_path),
        source_type="md",
        byte_size=alias_path.stat().st_size,
        metadata={"original_hash": "fixture"},
    )

    service = FoundationService(knowledge_system.settings.database_path)
    analytics = service.analytics(7)
    included = service.included_files()

    assert analytics["original_documents"] == 1
    assert analytics["source_aliases"] == 1
    assert analytics["represented_original_paths"] == 2
    assert included["canonical_total"] == 1
    assert included["alias_total"] == 1
    assert included["total"] == 2
    assert sum(bool(item["is_alias"]) for item in included["items"]) == 1
    assert included["level_counts"] == {"fulltext": 2}
    assert included["canonical_level_counts"] == {"fulltext": 1}


def test_l3_alias_promotes_canonical_source_and_requeues_existing_chunks(
    knowledge_system, source_root
):
    source = source_root / "canonical.md"
    alias_path = source_root / "copy" / "same-content.md"
    lower_level_alias = source_root / "copy" / "lower-level.md"
    alias_path.parent.mkdir()
    payload = "重复来源被选为L3后，规范资料也必须进入向量候选。"
    source.write_text(payload, encoding="utf-8")
    alias_path.write_text(payload, encoding="utf-8")
    lower_level_alias.write_text(payload, encoding="utf-8")
    stored = knowledge_system.ingestion.import_file(source, domain="work", privacy="private")

    with knowledge_system.database.connect() as connection:
        chunks = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM chunks WHERE source_id=?", (stored["source_id"],)
            )
        ]
        connection.execute(
            "UPDATE index_outbox SET status='deferred',last_error_code='outside_embedding_scope' "
            "WHERE entity_type='chunk' AND entity_id IN ("
            + ",".join("?" for _ in chunks)
            + ")",
            chunks,
        )
        connection.commit()

    canonical_result = knowledge_system.repository.register_source_alias(
        source_id=stored["source_id"],
        original_uri=str(source),
        original_name=source.name,
        vault_path=str(source),
        source_type="md",
        byte_size=source.stat().st_size,
        metadata={"requested_processing_level": "L3"},
    )
    with knowledge_system.database.connect() as connection:
        canonical_after_same_path = connection.execute(
            "SELECT metadata_json FROM sources WHERE id=?", (stored["source_id"],)
        ).fetchone()
        queued_after_same_path = connection.execute(
            "SELECT COUNT(*) FROM index_outbox WHERE event_key LIKE 'vector:upsert:%' "
            "AND entity_id IN (" + ",".join("?" for _ in chunks) + ") "
            "AND status='pending'",
            chunks,
        ).fetchone()[0]

    assert json.loads(canonical_after_same_path["metadata_json"])[
        "requested_processing_level"
    ] == "L3"
    assert queued_after_same_path == len(chunks)
    knowledge_system.repository.register_source_alias(
        source_id=stored["source_id"],
        original_uri=str(alias_path),
        original_name=alias_path.name,
        vault_path=str(alias_path),
        source_type="md",
        byte_size=alias_path.stat().st_size,
        metadata={"requested_processing_level": "L3"},
    )
    knowledge_system.repository.register_source_alias(
        source_id=stored["source_id"],
        original_uri=str(lower_level_alias),
        original_name=lower_level_alias.name,
        vault_path=str(lower_level_alias),
        source_type="md",
        byte_size=lower_level_alias.stat().st_size,
        metadata={"requested_processing_level": "L1"},
    )

    with knowledge_system.database.connect() as connection:
        canonical = connection.execute(
            "SELECT metadata_json FROM sources WHERE id=?", (stored["source_id"],)
        ).fetchone()
        queued = connection.execute(
            "SELECT COUNT(*) FROM index_outbox WHERE event_key LIKE 'vector:upsert:%' "
            "AND entity_id IN (" + ",".join("?" for _ in chunks) + ") "
            "AND status='pending'",
            chunks,
        ).fetchone()[0]
        chunk_count = connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_id=?", (stored["source_id"],)
        ).fetchone()[0]

    assert canonical_result == {"status": "canonical", "source_id": stored["source_id"]}
    assert json.loads(canonical["metadata_json"])["requested_processing_level"] == "L3"
    assert queued == len(chunks)
    assert chunk_count == len(chunks)


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
        for endpoint in [
            "overview",
            "ledger-summary",
            "analytics",
            "included-files",
            "documents",
            "scopes",
        ]:
            r = client.get(f"/api/foundation/{endpoint}")
            assert r.status_code == 200
        d = client.get("/api/foundation/scopes").json()["data"]
        assert d["authorized"] is False and d["scan_started"] is False
        assert client.get("/api/foundation/documents?limit=999").status_code == 422
        assert client.get("/api/foundation/files?root_id=bad&state=error").status_code == 400
        assert client.get("/api/foundation/files?root_id=bad&state=unknown").status_code == 422


def test_document_ledger_error_is_safe_and_actionable(test_settings, monkeypatch):
    def interrupted(*args, **kwargs):
        raise sqlite3.OperationalError("interrupted near C:/private-data/secret.sqlite")

    monkeypatch.setattr(FoundationService, "documents", interrupted)
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/foundation/documents")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "read_budget_reached"
    assert detail["retryable"] is True
    assert "private-data" not in str(detail)
