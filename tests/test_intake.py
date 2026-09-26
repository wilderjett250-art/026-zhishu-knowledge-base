import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.ingest import sha256_file
from pkas.intake import DEFAULT_RULES, IntakeRequest, IntakeService, extraction_quality


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


def test_extraction_quality_is_body_free_and_exposes_attention_signals():
    parsed = SimpleNamespace(
        parser_name="pymupdf-blocks",
        metadata={
            "extraction": {
                "initial_quality": {"score": 0.72, "reasons": ["low_text_or_scanned_pdf_pages"]},
                "warnings": ["paddleocr:remote_processing_not_enabled"],
                "block_index_truncated": True,
            }
        },
    )

    quality = extraction_quality(parsed)

    assert quality == {
        "status": "attention",
        "score": 0.72,
        "reasons": ["low_text_or_scanned_pdf_pages", "block_metadata_truncated"],
        "warnings": ["paddleocr:remote_processing_not_enabled"],
        "parser": "pymupdf-blocks",
    }


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


def test_semantic_intake_drains_vector_outbox_in_bounded_batches(
    knowledge_system, monkeypatch
):
    service = IntakeService(knowledge_system)
    pending = 8626
    calls = []

    def process(*, limit):
        nonlocal pending
        calls.append(limit)
        pending -= min(limit, pending)
        return {"status": "completed", "claimed": limit, "completed": limit}

    def coverage():
        return {"eligible": 8626, "indexed": 8626 - pending, "pending": pending}

    monkeypatch.setattr(knowledge_system.outbox, "process", process)
    monkeypatch.setattr(knowledge_system.rag.vector_index, "coverage", coverage)

    result, final_coverage, batches = service._sync_semantic_vectors()

    assert calls == [5000, 5000]
    assert result["status"] == "completed"
    assert batches == 2
    assert final_coverage["pending"] == 0


def test_retry_reuses_verified_recovery_point_when_drive_has_no_snapshot_space(
    knowledge_system, tmp_path, monkeypatch
):
    service = IntakeService(knowledge_system)
    plan_id = "a" * 32
    recovery_root = tmp_path / "recovery"
    recovery_file = recovery_root / plan_id / "pkas.sqlite"
    recovery_file.parent.mkdir(parents=True)
    with sqlite3.connect(recovery_file) as database:
        database.execute("CREATE TABLE recovery_marker(value TEXT)")
        database.execute("INSERT INTO recovery_marker VALUES ('verified')")
    plan = {
        "id": plan_id,
        "recovery_root": str(recovery_root),
        "recovery": str(recovery_file),
        "recovery_sha256": sha256_file(recovery_file),
        "items": [{"state": "pending", "action": "semantic", "bytes": 10}],
    }
    monkeypatch.setattr(
        "pkas.intake.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=100, used=100, free=0),
    )

    capacity = service._recovery_capacity(plan, plan["items"])
    service._backup(plan)

    assert capacity["ready"] is True
    assert capacity["required_free_bytes"] == 0
    assert capacity["existing_recovery_reused"] is True
    assert sha256_file(recovery_file) == plan["recovery_sha256"]


def test_semantic_preflight_is_aggregate_only_and_does_not_call_embedding(
    knowledge_system, source_root
):
    (source_root / "semantic.md").write_text("预检不应发送这段资料", encoding="utf-8")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root, rules=dict(DEFAULT_RULES, markdown="semantic"))

    viewed = service.view(plan["id"])

    assert viewed["semantic_preflight"] == {
        "required_confirmation": True,
        "pending_files": 1,
        "source_bytes": len("预检不应发送这段资料".encode()),
        "cloud_called": False,
        "remote_scope": "仅在确认后发送所选L3资料经本地解析得到的切片文本",
        "execution_order": [
            "先校验恢复空间并创建本地 SQLite 恢复点",
            "本地解析、切块并建立全文索引",
            "将切片文本发送给已配置的 Embedding 服务",
            "把返回向量写入本地向量索引并记录覆盖状态",
        ],
        "cost_estimate": {
            "status": "not_available",
            "reason": "本机未保存服务商单价或预计 Token 数；不会在预检阶段调用服务或产生费用。",
        },
    }
    assert service.read(plan["id"])["cloud_called"] is False


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


def test_extract_plan_reports_reused_excerpt_as_duplicate(knowledge_system, source_root):
    note = source_root / "excerpt.md"
    note.write_text("可复用摘录的状态必须如实显示。", encoding="utf-8")
    service = IntakeService(knowledge_system)
    rules = dict(DEFAULT_RULES, markdown="extract")

    first = execute(service, preview(service, source_root, rules=rules))
    second = execute(service, preview(service, source_root, rules=rules))

    assert first["items"][0]["state"] == "indexed"
    assert second["items"][0]["state"] == "duplicate"


def test_intake_outcome_reason_summary_is_path_free():
    assert IntakeService._outcome_reason("内容含疑似凭据，未入库") == "疑似凭据，已安全隔离"
    assert (
        IntakeService._outcome_reason("文件在分类后发生变化，请先增量复查")
        == "分类后文件已变化，需增量复查"
    )
    assert (
        IntakeService._outcome_reason(r"E:\private\unexpected failure")
        == "其他未处理异常，详情见单项状态"
    )


def test_split_l3_keeps_local_levels_runnable_without_vector_confirmation(
    knowledge_system, source_root
):
    (source_root / "semantic.md").write_text("需要单独确认的语义资料", encoding="utf-8")
    (source_root / "full.csv").write_text("名称,状态\n本地全文资料,就绪\n", encoding="utf-8")
    service = IntakeService(knowledge_system)
    rules = dict(DEFAULT_RULES, markdown="semantic", documents="full")
    plan = preview(service, source_root, rules=rules)

    primary = service.split_semantic(plan["id"])
    child_id = primary["deferred_semantic_plan_id"]
    child = service.read(child_id)

    assert primary["deferred_semantic_count"] == 1
    assert {item["state"] for item in primary["items"]} == {"pending", "deferred"}
    assert child["actions"] == {"semantic": 1}
    assert service.requires_vector(plan["id"]) is False
    assert service.requires_vector(child_id) is True
    with pytest.raises(ValueError, match="Embedding"):
        service.run(child_id, True, False)
    result = execute(service, primary)
    assert result["state"] == "completed"


def test_duplicate_content_keeps_each_original_path_as_provenance_alias(
    knowledge_system, source_root
):
    first = source_root / "first.md"
    second = source_root / "nested" / "second.md"
    second.parent.mkdir()
    payload = "同内容不同路径的资料，应只建立一份检索内容。"
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")

    service = IntakeService(knowledge_system)
    result = execute(service, preview(service, source_root))

    assert result["state"] == "completed"
    assert sorted(item["state"] for item in result["items"]) == ["duplicate", "indexed"]
    assert first.read_text(encoding="utf-8") == payload
    assert second.read_text(encoding="utf-8") == payload
    with knowledge_system.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        alias = connection.execute("SELECT * FROM source_aliases").fetchone()
        assert alias["original_uri"] in {str(first), str(second)}
        assert alias["source_type"] == "md"
        source = connection.execute("SELECT id FROM sources").fetchone()
        assert alias["source_id"] == source["id"]


def test_save_retries_a_transient_windows_plan_file_lock(knowledge_system, monkeypatch):
    service = IntakeService(knowledge_system)
    plan = {"id": "a" * 32, "state": "ready"}
    original_replace = os.replace
    attempts = 0

    def intermittently_locked(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporary local reader")
        return original_replace(source, target)

    monkeypatch.setattr("pkas.intake.os.replace", intermittently_locked)
    service._save(plan)

    assert attempts == 3
    assert service.read(plan["id"])["state"] == "ready"


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


def test_classified_manifest_revalidates_stale_file(knowledge_system, source_root, tmp_path):
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
            "recovery_root": str(tmp_path / "classified-recovery"),
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
    assert plan["recovery_root"] == str((tmp_path / "classified-recovery").resolve())
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
