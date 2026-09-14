import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.intake import DEFAULT_RULES, IntakeRequest, IntakeService


def wait(service):
    service.thread.join(20)
    assert not service.thread.is_alive()


def preview(service, root, **kwargs):
    plan = service.preview(IntakeRequest(path=str(root), **kwargs))
    wait(service)
    return service.read(plan["id"])


def execute(service, plan):
    service.run(plan["id"], True)
    wait(service)
    return service.read(plan["id"])


def test_semantic_mode_requires_separate_confirmation_and_runs_vector_sync(
    knowledge_system, source_root, monkeypatch
):
    (source_root / "semantic.md").write_text("语义向量接入验收内容", encoding="utf-8")
    service = IntakeService(knowledge_system)
    rules = dict(DEFAULT_RULES, markdown="semantic")
    plan = preview(service, source_root, rules=rules)
    with pytest.raises(ValueError, match="Embedding"):
        service.run(plan["id"], True, False)

    called = []

    def process(*, limit):
        called.append(limit)
        return {"status": "completed", "claimed": 1, "completed": 1}

    monkeypatch.setattr(knowledge_system.outbox, "process", process)
    monkeypatch.setattr(
        knowledge_system.rag.vector_index,
        "coverage",
        lambda: {"eligible": 1, "indexed": 1, "pending": 0, "coverage": 1.0},
    )
    service.run(plan["id"], True, True)
    wait(service)
    result = service.read(plan["id"])
    assert result["state"] == "completed"
    assert result["items"][0]["state"] == "indexed"
    assert result["items"][0]["vector"] == "向量同步已完成"
    assert result["vector_coverage"]["coverage"] == 1.0
    assert called == [5000]


def test_reference_import_and_search(knowledge_system, source_root):
    note = source_root / "note.md"
    note.write_text("唯一索引验收词 星河项目需求是离线查询资料。", encoding="utf-8")
    original = note.read_bytes()
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    assert plan["state"] == "ready"
    assert not knowledge_system.repository.search("唯一索引验收词")
    with pytest.raises(ValueError):
        service.run(plan["id"], False)
    result = execute(service, plan)
    assert result["state"] == "completed", result
    assert note.read_bytes() == original
    assert not list(service.settings.vault_root.rglob("*.md"))
    hits = knowledge_system.repository.search("唯一索引验收词")
    assert len(hits) == 1
    with knowledge_system.database.connect() as c:
        row = c.execute("select * from sources where id=?", (hits[0]["source_id"],)).fetchone()
        assert row["vault_path"] == str(note)
        assert json.loads(row["metadata_json"])["storage_mode"] == "reference"
        assert c.execute("select count(*) from index_outbox").fetchone()[0] > 0
    assert result["cloud_called"] is False
    second = execute(service, preview(service, source_root))
    assert second["items"][0]["state"] == "duplicate"


def test_exclusions_catalog_and_limit(knowledge_system, source_root):
    (source_root / "node_modules").mkdir()
    (source_root / "node_modules" / "a.md").write_text("excluded")
    (source_root / ".env").write_text("never-read")
    (source_root / "video.mp4").write_bytes(b"fake")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    assert len(plan["items"]) == 1
    result = execute(service, plan)
    assert result["items"][0]["state"] == "cataloged"
    assert not (service.home / "recovery").exists()
    (source_root / "b.md").write_text("second")
    plan = preview(service, source_root, max_files=1)
    assert plan["state"] == "limited"
    with pytest.raises(ValueError):
        service.run(plan["id"], True)


def test_file_changed_since_preview(knowledge_system, source_root):
    note = source_root / "a.md"
    note.write_text("before")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    note.write_text("after changed longer")
    result = execute(service, plan)
    assert result["state"] == "warning"
    assert result["items"][0]["state"] == "error"
    with knowledge_system.database.connect() as c:
        assert c.execute("select count(*) from sources").fetchone()[0] == 0


def test_classified_manifest_revalidates_stale_file(knowledge_system, source_root):
    note = source_root / "classified.md"
    note.write_text("before", encoding="utf-8")
    stat = note.stat()
    service = IntakeService(knowledge_system)
    plan = service.preview_classified(
        {
            "source_summary_job": "a" * 32,
            "root": str(source_root),
            "roots": [str(source_root)],
            "mode": "full",
            "category_ids": ["work_requirements"],
            "items": [
                {
                    "path": str(note),
                    "relative": note.name,
                    "bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "content_category_id": "work_requirements",
                    "content_category_label": "需求资料",
                    "classification_revision": 1,
                }
            ],
        }
    )
    assert plan["counts"] == {"pending": 1}
    note.write_text("changed after preview", encoding="utf-8")
    result = execute(service, service.read(plan["id"]))
    assert result["state"] == "warning"
    assert result["items"][0]["state"] == "error"


def test_extract_and_md_fallback(knowledge_system, source_root):
    (source_root / "a.txt").write_text("本地文字摘录验收。\n另一段内容。", encoding="utf-8")
    service = IntakeService(knowledge_system)
    rules = dict(DEFAULT_RULES, documents="extract")
    result = execute(service, preview(service, source_root, rules=rules))
    assert result["state"] == "completed"
    with knowledge_system.database.connect() as c:
        row = c.execute("select * from sources where source_type='local-extract'").fetchone()
        assert json.loads(row["metadata_json"])["coverage"] == "partial"
        assert "不是AI总结" in Path(row["vault_path"]).read_text(encoding="utf-8")
    rules = dict.fromkeys(DEFAULT_RULES, "md_fallback")
    result = execute(service, preview(service, source_root, rules=rules))
    assert result["map_source_id"]
    with knowledge_system.database.connect() as c:
        row = c.execute("select * from sources where id=?", (result["map_source_id"],)).fetchone()
        content = Path(row["vault_path"]).read_text(encoding="utf-8")
        assert "a.txt" in content and "本地文字摘录验收" not in content
    (source_root / "readme.md").write_text("existing project markdown")
    result = execute(service, preview(service, source_root, rules=rules))
    assert not result.get("map_source_id")
    assert any(i["state"] == "indexed" for i in result["items"])


def test_cancel_resume_without_duplicate(knowledge_system, source_root, monkeypatch):
    for n in range(3):
        (source_root / f"{n}.md").write_text(f"验收测试文件{n}", encoding="utf-8")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    original = service._document

    def stop_after_one(*args):
        value = original(*args)
        service.stop.set()
        return value

    monkeypatch.setattr(service, "_document", stop_after_one)
    result = execute(service, plan)
    assert result["state"] == "cancelled"
    assert sum(i["state"] == "indexed" for i in result["items"]) == 1
    recovered = IntakeService(knowledge_system)
    result = execute(recovered, recovered.read(plan["id"]))
    assert result["state"] == "completed"
    with knowledge_system.database.connect() as c:
        assert c.execute("select count(*) from sources").fetchone()[0] == 3


def test_api_confirmation_and_readonly_preview(test_settings, source_root):
    (source_root / "a.md").write_text("API intake fixture")
    with TestClient(create_app(test_settings)) as client:
        r = client.post("/api/foundation/intake/preview", json={"path": str(source_root)})
        assert r.status_code == 200
        job_id = r.json()["data"]["id"]
        wait(client.app.state.intake)
        assert client.get(f"/api/foundation/intake/{job_id}").json()["data"]["state"] == "ready"
        assert client.post(f"/api/foundation/intake/{job_id}/confirm").status_code == 409
        assert (
            client.post(f"/api/foundation/intake/{job_id}/confirm?confirmed=true").status_code
            == 200
        )
        wait(client.app.state.intake)
        assert client.get(f"/api/foundation/intake/{job_id}").json()["data"]["state"] == "completed"


def test_backup_failure_prevents_writes(knowledge_system, source_root, monkeypatch):
    (source_root / "a.md").write_text("backup gate fixture")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)

    def fail(_):
        raise OSError("synthetic no space")

    monkeypatch.setattr(service, "_backup", fail)
    result = execute(service, plan)
    assert result["state"] == "failed"
    with knowledge_system.database.connect() as c:
        assert c.execute("select count(*) from sources").fetchone()[0] == 0


def test_md_only_and_excluded_scope(knowledge_system, source_root):
    (source_root / "a.md").write_text("only markdown")
    (source_root / "b.txt").write_text("not imported")
    service = IntakeService(knowledge_system)
    result = execute(
        service, preview(service, source_root, rules=dict.fromkeys(DEFAULT_RULES, "md_only"))
    )
    assert sum(i["state"] == "indexed" for i in result["items"]) == 1
    assert sum(i["state"] == "excluded" for i in result["items"]) == 1
    with pytest.raises(ValueError):
        preview(service, service.settings.data_root)


def test_second_task_rejected(knowledge_system, source_root, monkeypatch):
    service = IntakeService(knowledge_system)
    barrier = threading.Event()
    monkeypatch.setattr(service, "_scan", lambda _: barrier.wait(5))
    try:
        service.preview(IntakeRequest(path=str(source_root)))
        with pytest.raises(ValueError):
            service.preview(IntakeRequest(path=str(source_root)))
    finally:
        barrier.set()
        wait(service)


def test_reindex_does_not_overwrite_old_reference(knowledge_system, source_root):
    note = source_root / "a.md"
    note.write_text("old reference version")
    service = IntakeService(knowledge_system)
    result = execute(service, preview(service, source_root))
    source_id = result["items"][0]["source_id"]
    with knowledge_system.database.connect() as c:
        c.execute("update documents set parser_version='old' where source_id=?", (source_id,))
        c.commit()
    note.write_text("different new original bytes")
    result = knowledge_system.ingestion.reindex_outdated()
    assert result["skipped"] == 1
    with knowledge_system.database.connect() as c:
        assert (
            c.execute(
                "select text_content from documents where source_id=?", (source_id,)
            ).fetchone()[0]
            == "old reference version"
        )
