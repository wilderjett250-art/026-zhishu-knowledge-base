import errno
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.catalog_classification import ALL_INSPECTED_SELECTION_ID, CatalogClassificationLedger
from pkas.intake import IntakeService
from pkas.summary_agent import RESULT_SCHEMA
from pkas.summary_jobs import (
    AGENT_RETRY_VALIDATION_FEEDBACK,
    SummaryEdit,
    SummaryJobRequest,
    SummaryJobs,
    SummaryPromotion,
)
from pkas.system import KnowledgeSystem


def seed_catalog(settings, source: Path):
    home = settings.data_root / "machine-catalog"
    home.mkdir(parents=True, exist_ok=True)
    path = home / "catalog.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE files(id INTEGER PRIMARY KEY,scope_path TEXT,path TEXT UNIQUE,
            relative_path TEXT,parent_path TEXT,top_group TEXT,name TEXT,extension TEXT,
            category TEXT,byte_size INTEGER,modified_ns INTEGER,state TEXT,last_job_id TEXT,
            first_seen_at TEXT,last_seen_at TEXT)"""
        )
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT,started_at TEXT)")
        db.execute("INSERT INTO jobs VALUES('test','completed','now')")
        stat = source.stat()
        db.execute(
            "INSERT INTO files VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(source.parent), str(source), source.name, str(source.parent), "_root",
                source.name, source.suffix, "document", stat.st_size, stat.st_mtime_ns,
                "active", "test", "now", "now",
            ),
        )


def test_classification_ledger_attaches_a_catalog_read_only(test_settings, tmp_path):
    source = tmp_path / "README.md"
    source.write_text("项目说明", encoding="utf-8")
    seed_catalog(test_settings, source)
    ledger = CatalogClassificationLedger(test_settings.data_root)
    with ledger.connect() as db:
        ledger._attach_catalog(db)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("UPDATE catalog.files SET name='changed' WHERE id=1")
        finally:
            ledger._detach_catalog(db)


def test_existing_preflight_rows_gain_explicit_privacy_and_review_fields(test_settings):
    home = test_settings.data_root / "catalog-classification"
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "ledger.sqlite") as db:
        db.execute(
            """CREATE TABLE local_preflight(
                catalog_file_id INTEGER PRIMARY KEY, byte_size INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL, prefix_sha256 TEXT, detected_type TEXT,
                coverage TEXT NOT NULL, category_id TEXT NOT NULL,
                classification_basis TEXT NOT NULL, confidence REAL NOT NULL,
                outcome TEXT NOT NULL, reason TEXT NOT NULL, inspected_at TEXT NOT NULL
            )"""
        )
        db.execute(
            "INSERT INTO local_preflight VALUES(1,20,10,NULL,'text','partial',"
            "'unresolved_other','local',0,'sampled','bounded_content_sample','now')"
        )
    ledger = CatalogClassificationLedger(test_settings.data_root)
    with ledger.connect() as db:
        row = db.execute(
            "SELECT privacy_classification,review_status,taxonomy_revision "
            "FROM local_preflight"
        ).fetchone()
    assert tuple(row) == ("restricted", "unreviewed", "")


def test_catalog_batch_reserves_a_files_and_records_derived_result(
    test_settings, tmp_path, monkeypatch
):
    source = tmp_path / "spec.txt"
    source.write_text("甲方乙方约定", encoding="utf-8")
    seed_catalog(test_settings, source)
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    job = jobs.create(SummaryJobRequest(scope="catalog_batch"), start=False)["id"]
    initial = jobs.view(job, limit=0)
    assert initial["scanner"] == "machine_catalog_batch"
    assert initial["counts"] == {"pending": 1}
    assert jobs.catalog_progress()["queued"] == 1
    jobs.resume(job)
    result = wait(jobs, job)
    assert result["stage"] == "done", result["message"]
    assert result["catalog_ledger"] == {"updated": 1, "warning": 0}
    assert jobs.catalog_progress()["inspected"] == 1
    counts = jobs.catalog_classification_counts()
    assert counts[0]["id"] == "finance_contract"
    assert counts[0]["recommendations"]["unavailable"] == 1
    assert counts[-1]["id"] == ALL_INSPECTED_SELECTION_ID
    assert counts[-1]["recommendations"]["unavailable"] == 1
    selection = jobs.catalog_promotion_selection(
        SummaryPromotion(category_ids=["finance_contract"], mode="full")
    )
    assert selection["source_summary_job"] == "catalog-classification-ledger"
    assert selection["items"][0]["path"] == str(source)
    unfiltered = jobs.catalog_promotion_selection(
        SummaryPromotion(category_ids=[ALL_INSPECTED_SELECTION_ID], mode="full")
    )
    assert unfiltered["include_all_inspected"] is True
    assert len(unfiltered["items"]) == 1
    with pytest.raises(ValueError, match="没有待分类"):
        jobs.create(SummaryJobRequest(scope="catalog_batch"), start=False)
    assert len(list(jobs.home.glob("*/queue.sqlite"))) == 1
    source.write_text("甲方乙方约定（已更新）", encoding="utf-8")
    stat = source.stat()
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        db.execute(
            "UPDATE files SET byte_size=?,modified_ns=? WHERE id=1",
            (stat.st_size, stat.st_mtime_ns),
        )
        db.execute("INSERT INTO jobs VALUES('test-rescan','completed','now2')")
    next_job = jobs.create(SummaryJobRequest(scope="catalog_batch"), start=False)
    assert next_job["catalog_batch"]["reserved"] == 1
    with sqlite3.connect(jobs.catalog_ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE catalog_file_id=1").fetchone()[0] == 1


def test_catalog_promotion_supports_l1_local_extract(test_settings, tmp_path, monkeypatch):
    source = tmp_path / "brief.txt"
    source.write_text("甲方乙方项目范围与交付边界。", encoding="utf-8")
    seed_catalog(test_settings, source)
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    job_id = jobs.create(SummaryJobRequest(scope="catalog_batch"), start=False)["id"]
    jobs.resume(job_id)
    completed = wait(jobs, job_id)
    assert completed["stage"] == "done"

    selection = jobs.catalog_promotion_selection(
        SummaryPromotion(category_ids=["finance_contract"], mode="extract")
    )
    preview = IntakeService(KnowledgeSystem.create(test_settings)).preview_classified(selection)

    assert preview["request"]["mode"] == "extract"
    assert preview["actions"] == {"extract": 1}
    assert preview["items"][0]["state"] == "pending"


def test_catalog_reservation_can_be_limited_to_auto_selected_directory(test_settings, tmp_path):
    wanted = tmp_path / "wanted.txt"
    excluded = tmp_path / "excluded.txt"
    wanted.write_text("甲方乙方", encoding="utf-8")
    excluded.write_text("不应进入本批", encoding="utf-8")
    seed_catalog(test_settings, wanted)
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        stat = excluded.stat()
        db.execute(
            "INSERT INTO files VALUES(2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(excluded.parent), str(excluded), excluded.name, str(excluded.parent), "other",
                excluded.name, excluded.suffix, "document", stat.st_size, stat.st_mtime_ns,
                "active", "test", "now", "now",
            ),
        )
    ledger = CatalogClassificationLedger(test_settings.data_root)
    rows = ledger.reserve(
        "a" * 32,
        50,
        directory_units=[{"scope_path": str(wanted.parent), "top_group": "_root"}],
    )
    assert [row["path"] for row in rows] == [str(wanted)]


def test_auto_scope_batch_samples_distinct_directories_first(test_settings, tmp_path):
    first_group = tmp_path / "first"
    second_group = tmp_path / "second"
    first_group.mkdir()
    second_group.mkdir()
    files = [
        first_group / "a.md", first_group / "README.md",
        second_group / "c.md", second_group / "d.md",
    ]
    for path in files:
        path.write_text("项目背景与用途", encoding="utf-8")
    seed_catalog(test_settings, files[0])
    catalog = test_settings.data_root / "machine-catalog" / "catalog.sqlite"
    with sqlite3.connect(catalog) as db:
        db.execute(
            "UPDATE files SET scope_path=?,top_group=? WHERE id=1",
            (str(tmp_path), "first"),
        )
        for identifier, path in enumerate(files[1:], start=2):
            stat = path.stat()
            db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier, str(tmp_path), str(path), path.name,
                    str(path.parent), path.parent.name, path.name, path.suffix,
                    "document", stat.st_size, stat.st_mtime_ns,
                    "active", "test", "now", "now",
                ),
            )
    ledger = CatalogClassificationLedger(test_settings.data_root)
    units = [
        {"scope_path": str(tmp_path), "top_group": "first"},
        {"scope_path": str(tmp_path), "top_group": "second"},
    ]
    rows = ledger.reserve(
        "e" * 32, 2, directory_units=units, automatic_only=True
    )
    assert [Path(row["path"]).parent.name for row in rows] == ["first", "second"]
    assert Path(rows[0]["path"]).name == "README.md"


def test_catalog_batch_can_select_a_current_provisional_category(
    test_settings, tmp_path, monkeypatch
):
    contract = tmp_path / "contract.md"
    requirements = tmp_path / "requirements.md"
    contract.write_text("甲方乙方签订合同", encoding="utf-8")
    requirements.write_text("功能需求与验收标准", encoding="utf-8")
    seed_catalog(test_settings, contract)
    stat = requirements.stat()
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        db.execute(
            "INSERT INTO files VALUES(2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(requirements.parent), str(requirements), requirements.name,
                str(requirements.parent), "_root", requirements.name,
                requirements.suffix, "document", stat.st_size,
                stat.st_mtime_ns, "active", "test", "now", "now",
            ),
        )
    ledger = CatalogClassificationLedger(test_settings.data_root)
    monkeypatch.setattr(ledger, "_safe_preflight_path", lambda path, scope: True)
    assert ledger.preflight_batch(2)["sampled"] == 2
    jobs = SummaryJobs(test_settings)
    selected = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch", provider="external", allow_remote_processing=True,
            local_category_id="work_requirements",
        ), start=False,
    )
    assert [Path(item["path"]).name for item in selected["records"]] == [
        "requirements.md"
    ]
    assert selected["request"]["local_category_id"] == "work_requirements"
    before = set(jobs.home.glob("*/queue.sqlite"))
    with pytest.raises(ValueError, match="没有待分类"):
        jobs.create(
            SummaryJobRequest(
                scope="catalog_batch", provider="external",
                allow_remote_processing=True,
                local_category_id="work_requirements",
            ), start=False,
        )
    assert set(jobs.home.glob("*/queue.sqlite")) == before
    assert ledger.reserve(
        "f" * 32, 1, automatic_only=True, remote_processing=True,
        local_category_id="finance_contract",
    )[0]["path"] == str(contract)


def test_catalog_reservation_prioritizes_readable_material_before_binary_files(
    test_settings, tmp_path
):
    source = tmp_path / "package.zip"
    source.write_bytes(b"binary archive")
    seed_catalog(test_settings, source)
    readable = tmp_path / "requirements.md"
    readable.write_text("项目需求和交付边界", encoding="utf-8")
    image = tmp_path / "diagram.png"
    image.write_bytes(b"image bytes")
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        for identifier, path in ((2, readable), (3, image)):
            stat = path.stat()
            db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier, str(path.parent), str(path), path.name, str(path.parent), "_root",
                    path.name, path.suffix, "document", stat.st_size, stat.st_mtime_ns,
                    "active", "test", "now", "now",
                ),
            )
    rows = CatalogClassificationLedger(test_settings.data_root).reserve("c" * 32, 3)
    assert [Path(row["path"]).name for row in rows] == [
        "requirements.md", "diagram.png", "package.zip"
    ]


def test_auto_reservation_keeps_runtime_logs_in_catalog_only(test_settings, tmp_path):
    log = tmp_path / "runtime.log"
    log.write_text("transient output", encoding="utf-8")
    seed_catalog(test_settings, log)
    error_log = tmp_path / "runtime.err"
    error_log.write_text("transient error", encoding="utf-8")
    binary = tmp_path / "library.so"
    binary.write_bytes(b"binary library")
    document = tmp_path / "requirements.md"
    document.write_text("项目需求", encoding="utf-8")
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        for identifier, path in ((2, document), (3, error_log), (4, binary)):
            stat = path.stat()
            db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier, str(path.parent), str(path), path.name, str(path.parent),
                    "_root", path.name, path.suffix, "document", stat.st_size,
                    stat.st_mtime_ns, "active", "test", "now", "now",
                ),
            )
    rows = CatalogClassificationLedger(test_settings.data_root).reserve(
        "d" * 32, 50, automatic_only=True
    )
    assert [Path(row["path"]).name for row in rows] == ["requirements.md"]


def test_scoped_auto_reservation_reaches_document_after_many_l0_files(
    test_settings, tmp_path
):
    first_log = tmp_path / "run-000.log"
    first_log.write_text("generated", encoding="utf-8")
    seed_catalog(test_settings, first_log)
    document = tmp_path / "PROJECT.md"
    document.write_text("项目背景、做法和交付边界。", encoding="utf-8")
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        for identifier in range(2, 82):
            path = tmp_path / f"run-{identifier:03d}.log"
            path.write_text("generated", encoding="utf-8")
            stat = path.stat()
            db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier, str(tmp_path), str(path), path.name, str(tmp_path),
                    "_root", path.name, path.suffix, "document", stat.st_size,
                    stat.st_mtime_ns, "active", "test", "now", "now",
                ),
            )
        stat = document.stat()
        db.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                82, str(tmp_path), str(document), document.name, str(tmp_path),
                "_root", document.name, document.suffix, "document", stat.st_size,
                stat.st_mtime_ns, "active", "test", "now", "now",
            ),
        )
    ledger = CatalogClassificationLedger(test_settings.data_root)
    rows = ledger.reserve(
        "e" * 32, 1,
        directory_units=[{"scope_path": str(tmp_path), "top_group": "_root"}],
        automatic_only=True,
    )
    assert [Path(row["path"]).name for row in rows] == ["PROJECT.md"]


def test_catalog_progress_distinguishes_ai_decisions_from_local_skips(
    test_settings, tmp_path, monkeypatch
):
    document = tmp_path / "requirements.md"
    document.write_text("项目需求", encoding="utf-8")
    runtime = tmp_path / "runtime.log"
    runtime.write_text("transient output", encoding="utf-8")
    seed_catalog(test_settings, document)
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        stat = runtime.stat()
        db.execute(
            "INSERT INTO files VALUES(2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(runtime.parent), str(runtime), runtime.name, str(runtime.parent), "_root",
                runtime.name, runtime.suffix, "document", stat.st_size, stat.st_mtime_ns,
                "active", "test", "now", "now",
            ),
        )
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    job_id = jobs.create(SummaryJobRequest(scope="catalog_batch"), start=False)["id"]
    progress = jobs.catalog_progress()
    assert progress["catalog_active"] == 2
    assert progress["ai_eligible"] == 1
    assert progress["ai_pending"] == 1
    assert progress["decision_coverage_percent"] == 50.0

    # A local-only inspection is not an AI decision; the ledger must keep it
    # pending rather than pretending that the local classifier understood it.
    jobs.resume(job_id)
    result = wait(jobs, job_id)
    assert result["stage"] == "done"
    progress = jobs.catalog_progress()
    assert progress["ai_done"] == 0
    assert progress["ai_pending"] == 1
    assert progress["ai_failed"] == 0
    assert progress["local_skipped"] == 1
    assert progress["decision_done"] == 1
    assert progress["decision_coverage_percent"] == 50.0


def test_catalog_ledger_reinitialization_preserves_audited_ai_state(test_settings, tmp_path):
    source = tmp_path / "README.md"
    source.write_text("项目说明：背景、做法与交付。", encoding="utf-8")
    seed_catalog(test_settings, source)
    ledger = CatalogClassificationLedger(test_settings.data_root)
    ledger.reserve("a" * 32, 1, automatic_only=True)
    with ledger.connect() as db:
        db.execute(
            "UPDATE entries SET state='inspected',ai_state='local_l0' WHERE catalog_file_id=1"
        )
    reopened = CatalogClassificationLedger(test_settings.data_root)
    with reopened.connect() as db:
        state = db.execute("SELECT ai_state FROM entries WHERE catalog_file_id=1").fetchone()[0]
        assert state == "local_l0"


def test_catalog_scope_retry_route_stays_local_and_reports_progress(test_settings, tmp_path):
    source = tmp_path / "README.md"
    source.write_text("项目背景与说明。", encoding="utf-8")
    seed_catalog(test_settings, source)
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/foundation/summary-jobs/catalog-progress/retry", json={})
        assert response.status_code == 200
        assert response.json()["data"]["scope_state"] in {"building", "ready"}
        denied = client.post(
            "/api/foundation/summary-jobs/catalog-progress/retry",
            json={}, headers={"origin": "https://external.example"},
        )
        assert denied.status_code == 403


def test_local_preflight_is_resumable_and_keeps_body_out_of_ledger(
    test_settings, tmp_path, monkeypatch
):
    source = tmp_path / "contract.md"
    source.write_text("甲方乙方项目交付合同。", encoding="utf-8")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot a readable document")
    seed_catalog(test_settings, source)
    stat = broken.stat()
    catalog = test_settings.data_root / "machine-catalog" / "catalog.sqlite"
    with sqlite3.connect(catalog) as db:
        db.execute(
            "INSERT INTO files VALUES(2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(broken.parent), str(broken), broken.name, str(broken.parent),
                "_root", broken.name, broken.suffix, "document", stat.st_size,
                stat.st_mtime_ns, "active", "test", "now", "now",
            ),
        )
    ledger = CatalogClassificationLedger(test_settings.data_root)
    monkeypatch.setattr(ledger, "_safe_preflight_path", lambda path, scope: True)
    result = ledger.preflight_batch(2)
    assert result["processed"] == 2
    assert result["sampled"] == 1
    assert result["needs_review"] == 1
    assert ledger.preflight_batch(2)["processed"] == 0
    progress = ledger.progress()
    assert progress["local_read_done"] == 2
    assert progress["local_read_pending"] == 0
    assert progress["ai_done"] == 0
    assert progress["ai_failed"] == 0
    assert progress["review_pending"] == 1
    categories = ledger.preflight_categories()
    assert categories["status"] == "local_provisional_not_ai"
    assert categories["total"] == 1
    assert categories["categories"][0]["id"] == "finance_contract"
    reviews = ledger.preflight_reviews()
    assert reviews["total"] == 1
    assert reviews["reasons"] == [{"reason": "structure_parse_failed", "total": 1}]
    assert Path(reviews["items"][0]["path"]).name == "broken.pdf"
    with ledger.connect() as db:
        row = db.execute(
            "SELECT category_id,outcome,reason,privacy_classification,review_status "
            "FROM local_preflight "
            "WHERE catalog_file_id=1"
        ).fetchone()
        assert dict(row) == {
            "category_id": "finance_contract", "outcome": "sampled",
            "reason": "bounded_content_sample",
            "privacy_classification": "restricted", "review_status": "unreviewed",
        }
        rows = db.execute("SELECT * FROM local_preflight").fetchall()
        assert "甲方乙方" not in json.dumps([tuple(row) for row in rows], ensure_ascii=False)

    # Rebuilding the profile gate must preserve a matching content version.
    config = test_settings.data_root / "config" / "processing_profile.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({
            "rules": {"markdown": "full", "documents": "extract", "code": "catalog",
                      "images": "catalog", "other": "catalog"},
            "exclusions": ["unused-folder"],
        }), encoding="utf-8",
    )
    ledger.refresh_auto_scope()
    assert ledger.progress()["local_read_done"] == 2
    jobs = SummaryJobs(test_settings)
    queued = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch", provider="external", allow_remote_processing=True
        ), start=False,
    )
    assert queued["catalog_batch"]["reserved"] == 1
    assert Path(queued["records"][0]["path"]).name == "contract.md"


def test_local_preflight_never_classifies_a_stale_catalog_version(
    test_settings, tmp_path, monkeypatch
):
    source = tmp_path / "notes.md"
    source.write_text("旧内容", encoding="utf-8")
    seed_catalog(test_settings, source)
    source.write_text("新的文件内容，与目录索引时不同", encoding="utf-8")
    ledger = CatalogClassificationLedger(test_settings.data_root)
    monkeypatch.setattr(ledger, "_safe_preflight_path", lambda path, scope: True)
    assert ledger.preflight_batch(1)["needs_review"] == 1
    with ledger.connect() as db:
        row = db.execute(
            "SELECT outcome,reason,category_id FROM local_preflight"
        ).fetchone()
    assert tuple(row) == (
        "needs_review", "source_changed_since_catalog", "unresolved_other"
    )
    assert ledger.preflight_reviews()["reasons"] == [
        {"reason": "source_changed_since_catalog", "total": 1}
    ]


def test_local_preflight_flushes_a_partial_batch_before_pause(
    test_settings, tmp_path, monkeypatch
):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("第一份项目需求说明", encoding="utf-8")
    second.write_text("第二份项目需求说明", encoding="utf-8")
    seed_catalog(test_settings, first)
    stat = second.stat()
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        db.execute(
            "INSERT INTO files VALUES(2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(second.parent), str(second), second.name, str(second.parent),
                "_root", second.name, second.suffix, "document", stat.st_size,
                stat.st_mtime_ns, "active", "test", "now", "now",
            ),
        )
    ledger = CatalogClassificationLedger(test_settings.data_root)
    monkeypatch.setattr(ledger, "_safe_preflight_path", lambda path, scope: True)
    from pkas.file_inspector import inspect_file as original_inspect

    stop = threading.Event()

    def inspect_then_pause(path):
        result = original_inspect(path)
        stop.set()
        return result

    monkeypatch.setattr("pkas.catalog_classification.inspect_file", inspect_then_pause)
    assert ledger.preflight_batch(2, stop=stop)["processed"] == 1
    with ledger.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM local_preflight").fetchone()[0] == 1
    monkeypatch.setattr("pkas.catalog_classification.inspect_file", original_inspect)
    assert ledger.preflight_batch(2)["processed"] == 1
    assert ledger.progress()["local_read_pending"] == 0


def test_taxonomy_edit_marks_old_local_classification_stale_and_rereads(
    test_settings, tmp_path, monkeypatch
):
    from pkas.content_taxonomy import defaults

    source = tmp_path / "contract.md"
    source.write_text("甲方乙方签字确认", encoding="utf-8")
    seed_catalog(test_settings, source)
    ledger = CatalogClassificationLedger(test_settings.data_root)
    monkeypatch.setattr(ledger, "_safe_preflight_path", lambda path, scope: True)
    assert ledger.preflight_batch(1)["sampled"] == 1
    assert ledger.preflight_categories()["categories"][0]["id"] == "finance_contract"

    categories = defaults()
    for category in categories:
        if category["id"] == "finance_contract":
            category["keywords"] = []
        elif category["id"] == "work_requirements":
            category["keywords"].append("甲方乙方")
    config = test_settings.data_root / "config" / "content-taxonomy.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(categories, ensure_ascii=False), encoding="utf-8")
    stale = ledger.preflight_categories()
    assert stale["total"] == 0
    assert stale["stale_total"] == 1
    assert ledger.preflight_files("finance_contract")["total"] == 0
    assert ledger.preflight_batch(1)["sampled"] == 1
    current = ledger.preflight_categories()
    assert current["stale_total"] == 0
    assert current["categories"][0]["id"] == "work_requirements"
    assert ledger.preflight_files("work_requirements")["total"] == 1
    assert ledger.preflight_batch(1)["processed"] == 0


def test_catalog_preflight_api_is_local_only_and_reads_on_request(
    test_settings, tmp_path, monkeypatch
):
    source = tmp_path / "spec.md"
    source.write_text("功能需求：交付功能。", encoding="utf-8")
    seed_catalog(test_settings, source)
    with TestClient(create_app(test_settings)) as client:
        monkeypatch.setattr(
            client.app.state.summary_jobs.catalog_ledger,
            "_safe_preflight_path", lambda path, scope: True,
        )
        before = client.get("/api/foundation/summary-jobs/catalog-progress").json()["data"]
        assert before["local_read_done"] == 0
        for _ in range(100):
            scope = client.get(
                "/api/foundation/summary-jobs/catalog-progress"
            ).json()["data"]
            if scope["scope_state"] == "ready":
                break
            time.sleep(0.05)
        assert scope["scope_state"] == "ready"
        denied = client.post(
            "/api/foundation/summary-jobs/catalog-preflight/start",
            json={}, headers={"origin": "https://external.example"},
        )
        assert denied.status_code == 403
        assert client.post(
            "/api/foundation/summary-jobs/catalog-preflight/start", json={}
        ).status_code == 200
        for _ in range(100):
            state = client.get(
                "/api/foundation/summary-jobs/catalog-preflight"
            ).json()["data"]
            if not state["running"]:
                break
            time.sleep(0.05)
        assert state["state"] == "completed"
        after = client.get("/api/foundation/summary-jobs/catalog-progress").json()["data"]
        assert after["local_read_done"] == 1
        assert after["ai_done"] == 0
        categories = client.get(
            "/api/foundation/summary-jobs/catalog-preflight/categories"
        ).json()["data"]
        assert categories["total"] == 1
        files = client.get(
            "/api/foundation/summary-jobs/catalog-preflight/files",
            params={"category_id": "work_requirements", "limit": 1},
        ).json()["data"]
        assert files["total"] == 1
        assert files["items"][0]["path"] == str(source)
        assert files["items"][0]["classification_basis"] == "content_keywords"
        assert files["items"][0]["source_current"] is True
        assert "text_preview" not in files["items"][0]
        parent_files = client.get(
            "/api/foundation/summary-jobs/catalog-preflight/files",
            params={"category_id": "work", "offset": 1, "limit": 1},
        ).json()["data"]
        assert parent_files["total"] == 1
        assert parent_files["items"] == []
        assert client.get(
            "/api/foundation/summary-jobs/catalog-preflight/files",
            params={"category_id": "work"},
            headers={"origin": "https://external.example"},
        ).status_code == 403
        assert client.get(
            "/api/foundation/summary-jobs/catalog-preflight/files",
            params={"category_id": "deleted_category"},
        ).status_code == 409
        source.write_text("原文件随后发生了变化", encoding="utf-8")
        stale_file = client.get(
            "/api/foundation/summary-jobs/catalog-preflight/files",
            params={"category_id": "work_requirements"},
        ).json()["data"]["items"][0]
        assert stale_file["source_current"] is False
        assert client.get(
            "/api/foundation/summary-jobs/catalog-preflight/reviews"
        ).json()["data"]["total"] == 0


def test_unreadable_document_is_not_counted_as_ai_understood(
    test_settings, tmp_path, monkeypatch
):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"%PDF-1.4\nnot a readable document")
    seed_catalog(test_settings, source)
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    job_id = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch", provider="external", allow_remote_processing=True
        )
    )["id"]
    result = wait(jobs, job_id)
    assert result["stage"] == "done"
    assert result["records"][0]["ai_state"] == "unavailable"
    progress = jobs.catalog_progress()
    assert progress["ai_done"] == 0
    assert progress["ai_failed"] == 0
    assert progress["review_pending"] == 1


def test_catalog_agent_packet_hides_parent_directories(test_settings, tmp_path, monkeypatch):
    first_dir = tmp_path / "private-work"
    second_dir = tmp_path / "other-work"
    first_dir.mkdir()
    second_dir.mkdir()
    first = first_dir / "README.md"
    second = second_dir / "PROJECT.md"
    first.write_text("第一份项目背景与方案。", encoding="utf-8")
    second.write_text("第二份项目背景与方案。", encoding="utf-8")
    seed_catalog(test_settings, first)
    stat = second.stat()
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        db.execute(
            "INSERT INTO files VALUES(2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(second_dir), str(second), second.name, str(second_dir), "_root",
                second.name, second.suffix, "document", stat.st_size, stat.st_mtime_ns,
                "active", "test", "now", "now",
            ),
        )
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    job_id = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch", provider="external", allow_remote_processing=True,
            allow_restricted_remote_processing=True,
            catalog_batch_size=2,
        )
    )["id"]
    assert wait(jobs, job_id)["state"] == "awaiting_agent"
    packet = jobs.packet(job_id)
    assert {item["name"] for item in packet["items"]} == {"README.md", "PROJECT.md"}
    assert all("private-work" not in item["name"] for item in packet["items"])


@pytest.mark.parametrize("allow_restricted", [False, True])
@pytest.mark.parametrize("body", ["甲方乙方项目交付合同", "普通资料的具体用途尚未辨明"])
def test_restricted_file_requires_separate_remote_consent_and_redacts_sample(
    test_settings, tmp_path, monkeypatch, allow_restricted, body
):
    source = tmp_path / "contract.md"
    synthetic_token = "sk-" + "A" * 24
    source.write_text(f"{body}，token={synthetic_token}", encoding="utf-8")
    seed_catalog(test_settings, source)
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    job_id = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch", provider="external", allow_remote_processing=True,
            allow_restricted_remote_processing=allow_restricted,
        ), start=False,
    )["id"]
    jobs._inspect(job_id, threading.Event())
    record = jobs.view(job_id)["records"][0]
    assert record["record"]["privacy_classification"] == "restricted"
    packet = jobs.packet(job_id)
    if not allow_restricted:
        assert record["ai_state"] == "restricted_local_only"
        assert packet is None
    else:
        assert record["ai_state"] == "pending"
        assert packet is not None
        assert synthetic_token not in json.dumps(packet)
        assert "[REDACTED]" in packet["items"][0]["sample"]


def test_remote_triage_keeps_no_text_file_in_agent_queue(test_settings, source_root):
    binary = source_root / "library.so"
    binary.write_bytes(b"\x00\xff\x01\x00\x80\x00")
    jobs = SummaryJobs(test_settings)
    job_id = jobs.create(
        SummaryJobRequest(path=str(source_root), provider="external", allow_remote_processing=True)
    )["id"]
    assert wait(jobs, job_id)["state"] == "awaiting_agent"
    packet = jobs.packet(job_id)
    assert packet["items"][0]["metadata"]["sample_available"] is False
    item = packet["items"][0]
    with pytest.raises(ValueError, match="只能标记为未解析"):
        jobs.accept(
            job_id,
            packet["packet_id"],
            {"items": [{
                "id": item["id"], "summary": "二进制资料", "purpose": "二进制文件",
                "category_id": "unresolved_other", "secondary_category_ids": [],
                "importance": "low", "recommended_mode": "full",
                "recommendation_reason": "需要正文", "evidence": "",
                "uncertainty": "没有正文样本。",
            }]},
        )
    jobs.accept(
        job_id,
        packet["packet_id"],
        {"items": [{
            "id": item["id"], "summary": "正文未抽取，暂保留索引",
            "purpose": "正文未抽取，暂保留索引", "category_id": "unresolved_other",
            "secondary_category_ids": [], "importance": "low", "recommended_mode": "catalog",
            "recommendation_reason": "正文未抽取，暂保留索引", "evidence": "",
            "uncertainty": "未读取正文，仅完成元数据速判。",
        }]},
    )
    jobs.resume(job_id)
    assert wait(jobs, job_id)["ai_counts"]["done"] == 1


def test_auto_promotion_batch_requires_luna_and_reserves_only_selected_scope(
    test_settings, tmp_path, monkeypatch
):
    source = tmp_path / "candidate.txt"
    source.write_text("甲方乙方", encoding="utf-8")
    seed_catalog(test_settings, source)

    class FakePromotion:
        def __init__(self, *_args):
            pass

        def inspection_scope(self, job_id):
            assert job_id == "b" * 32
            return {
                "auto_promotion_id": job_id,
                "overview_id": "c" * 32,
                "units": [{"scope_path": str(source.parent), "top_group": "_root"}],
            }

    monkeypatch.setattr("pkas.summary_jobs.AutoPromotionService", FakePromotion)
    monkeypatch.setattr("pkas.summary_agent.codex_binary", lambda _: Path("C:/codex.exe"))
    jobs = SummaryJobs(test_settings)
    with pytest.raises(ValueError, match="必须明确使用Luna"):
        jobs.create(
            SummaryJobRequest(scope="auto_promotion_batch", auto_promotion_id="b" * 32),
            start=False,
        )
    job = jobs.create(
        SummaryJobRequest(
            scope="auto_promotion_batch",
            auto_promotion_id="b" * 32,
            provider="codex",
            allow_remote_processing=True,
        ),
        start=False,
    )
    assert job["scanner"] == "machine_catalog_batch"
    assert job["auto_promotion"]["id"] == "b" * 32
    assert job["catalog_batch"]["reserved"] == 1
    assert job["request"]["batch_size"] == 1


def wait(jobs, ident):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        data = jobs.view(ident)
        if not data["running"]:
            return data
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_local_md_and_safe_export(test_settings, source_root, tmp_path):
    file = source_root / "contract.txt"
    file.write_text("甲方乙方违约责任", encoding="utf-8")
    original = hashlib.sha256(file.read_bytes()).hexdigest()
    jobs = SummaryJobs(test_settings)
    job = jobs.create(SummaryJobRequest(path=str(source_root)))["id"]
    result = wait(jobs, job)
    assert result["stage"] == "done", result["message"]
    assert result["records"][0]["record"]["classification"]["category_id"] == "finance_contract"
    assert len(list(Path(result["md_path"]).glob("*.md"))) == 2
    with pytest.raises(ValueError, match="源范围"):
        jobs.export(job, str(source_root))
    destination = tmp_path / "obsidian"
    destination.mkdir()
    exports = [jobs.export(job, str(destination))["path"] for _ in range(2)]
    assert exports[0] != exports[1]
    assert hashlib.sha256(file.read_bytes()).hexdigest() == original
    row = result["records"][0]
    jobs.edit(
        job,
        row["id"],
        SummaryEdit(category_id="work_requirements", summary="用户修改", revision=row["revision"]),
    )
    assert jobs.view(job)["records"][0]["summary"]["text"] == "用户修改"
    with pytest.raises(ValueError):
        jobs.edit(
            job,
            row["id"],
            SummaryEdit(category_id="work_requirements", summary="覆盖", revision=row["revision"]),
        )


def test_pause_resume_does_not_reread_completed(test_settings, source_root, monkeypatch):
    for n in range(3):
        (source_root / f"{n}.txt").write_text("甲方乙方", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(SummaryJobRequest(path=str(source_root)), start=False)["id"]
    stop = threading.Event()
    jobs._discover(job, stop)
    from pkas.file_inspector import inspect_file

    calls = []

    def inspect(path):
        calls.append(path)
        stop.set()
        return inspect_file(path)

    monkeypatch.setattr("pkas.summary_jobs.inspect_file", inspect)
    jobs._inspect(job, stop)
    assert len(calls) == 1
    resumed_calls = []

    def resumed_inspect(path):
        resumed_calls.append(path)
        return inspect_file(path)

    monkeypatch.setattr("pkas.summary_jobs.inspect_file", resumed_inspect)
    reopened = SummaryJobs(test_settings)
    reopened.resume(job)
    result = wait(reopened, job)
    assert result["counts"]["inspected"] == 3
    assert result["stage"] == "done"
    assert len(calls) == 1
    assert len(resumed_calls) == 2
    assert calls[0] not in resumed_calls


def test_resume_rechecks_changed_versions_and_invalidates_packet(
    test_settings, source_root, monkeypatch
):
    from pkas.file_inspector import inspect_file

    for n in range(2):
        (source_root / f"{n}.txt").write_text("甲方乙方", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(
        SummaryJobRequest(
            path=str(source_root), provider="external", allow_remote_processing=True,
            allow_restricted_remote_processing=True,
        )
    )["id"]
    wait(jobs, job)
    old = jobs.packet(job)
    (source_root / "0.txt").write_text("合同编号更新", encoding="utf-8")
    calls = []

    def inspect(path):
        calls.append(path.name)
        return inspect_file(path)

    monkeypatch.setattr("pkas.summary_jobs.inspect_file", inspect)
    jobs.resume(job)
    wait(jobs, job)
    assert calls == ["0.txt"]
    with pytest.raises(ValueError, match="失效"):
        jobs.accept(job, old["packet_id"], {})
    assert jobs.packet(job)["packet_id"] != old["packet_id"]


def test_markdown_does_not_embed_remote_images():
    from pkas.summary_jobs import markdown_literal

    text = markdown_literal("![tracking](https://example.org/pixel)<script>x</script>")
    assert "![" not in text
    assert "<script>" not in text


def test_read_error_can_be_retried_without_restarting_all(
    test_settings, source_root, monkeypatch
):
    # This test deliberately simulates a disappearing source.  Its fixture lives under
    # the platform temp folder, so relax the source-location guard only for this test.
    monkeypatch.setattr("pkas.directory_summary.EXCLUDED", frozenset())
    monkeypatch.setattr("pkas.directory_summary.user_documents_path", lambda: source_root)
    monkeypatch.setattr("pkas.summary_jobs.EXCLUDED", frozenset())
    file = source_root / "disappearing.txt"
    file.write_text("甲方乙方", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(SummaryJobRequest(path=str(source_root)), start=False)["id"]
    jobs._discover(job, threading.Event())
    file.unlink()
    jobs.resume(job)
    failed = wait(jobs, job)
    assert failed["counts"]["missing"] == 1
    assert failed["records"][0]["record"]["failure_kind"] == "missing"
    file.write_text("甲方乙方", encoding="utf-8")
    jobs.retry_errors(job)
    result = wait(jobs, job)
    assert result["counts"] == {"inspected": 1}
    assert result["stage"] == "done"


def test_inspection_failure_kind_redacts_platform_error_text():
    from pkas.summary_jobs import inspection_failure_kind

    assert inspection_failure_kind(FileNotFoundError(errno.ENOENT, "private path")) == "missing"
    assert inspection_failure_kind(OSError("boundary changed")) == "boundary_changed"
    assert inspection_failure_kind(OSError("changed during read")) == "changed_during_read"
    assert (
        inspection_failure_kind(PermissionError(errno.EACCES, "private path"))
        == "permission_denied"
    )


def test_external_agent_validates_evidence_and_deduplicates(test_settings, source_root):
    (source_root / "data.txt").write_text("甲方乙方约定", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(
        SummaryJobRequest(
            path=str(source_root), provider="external", allow_remote_processing=True,
            allow_restricted_remote_processing=True,
        )
    )["id"]
    assert wait(jobs, job)["state"] == "awaiting_agent"
    packet = jobs.packet(job)
    assert packet["items"][0]["local_preclassification"]["category_id"] == "finance_contract"
    assert packet["items"][0]["review_instruction"]
    response = {
        "items": [
            {
                "id": packet["items"][0]["id"],
                "summary": "约定事项",
                "evidence": "虚构",
                "category_id": "finance_contract",
                "uncertainty": "仅抽样",
            }
        ]
    }
    with pytest.raises(ValueError, match="证据"):
        jobs.accept(job, packet["packet_id"], response)
    response["items"][0]["evidence"] = "甲方乙方"
    jobs.accept(job, packet["packet_id"], response)
    jobs.accept(job, packet["packet_id"], response)
    assert jobs.view(job)["ai_batches"] == 1
    assert jobs.packet(job) is None
    jobs.resume(job)
    result = wait(jobs, job)
    assert result["stage"] == "done"
    assert result["ai_counts"]["done"] == 1


def test_ai_understanding_profile_supports_multi_category_and_unfiltered_promotion(
    test_settings, source_root, monkeypatch
):
    source = source_root / "proposal.txt"
    source.write_text("甲方提出项目交付方案与验收范围", encoding="utf-8")
    seed_catalog(test_settings, source)
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    job_id = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch", provider="external", allow_remote_processing=True,
            allow_restricted_remote_processing=True,
            catalog_batch_size=1,
        )
    )["id"]
    assert wait(jobs, job_id)["state"] == "awaiting_agent"
    packet = jobs.packet(job_id)
    jobs.accept(
        job_id,
        packet["packet_id"],
        {
            "items": [
                {
                    "id": packet["items"][0]["id"],
                    "summary": "项目交付与验收范围说明",
                    "purpose": "说明项目交付方案和验收范围",
                    "category_id": "work_requirements",
                    "secondary_category_ids": ["finance_contract"],
                    "importance": "high",
                    "recommended_mode": "semantic",
                    "recommendation_reason": "包含项目交付方案与验收范围",
                    "evidence": "项目交付方案与验收范围",
                    "uncertainty": "仅依据抽样，未读取附件。",
                }
            ]
        },
    )
    jobs.resume(job_id)
    result = wait(jobs, job_id)
    record = result["records"][0]["record"]
    assert record["understanding"]["purpose"] == "说明项目交付方案和验收范围"
    assert record["understanding"]["recommended_mode"] == "full"
    assert record["understanding"]["model_recommended_mode"] == "semantic"
    assert record["understanding"]["profile_cap_applied"] is True
    assert record["classification"]["secondary_category_ids"] == ["finance_contract"]

    secondary = jobs.promotion_selection(
        job_id, SummaryPromotion(category_ids=["finance_contract"], mode="full")
    )
    assert secondary["items"][0]["ai_understanding"]["importance"] == "high"
    recommended = jobs.promotion_selection(
        job_id, SummaryPromotion(category_ids=["work_requirements"], mode="recommended")
    )
    preview = IntakeService(KnowledgeSystem.create(test_settings)).preview_classified(recommended)
    assert preview["request"]["mode"] == "recommended"
    assert preview["actions"] == {"full": 1}
    assert preview["items"][0]["state"] == "pending"
    catalog_recommended = jobs.catalog_promotion_selection(
        SummaryPromotion(category_ids=["work_requirements"], mode="recommended")
    )
    assert catalog_recommended["items"][0]["recommended_action"] == "full"
    all_checked = jobs.promotion_selection(
        job_id,
        SummaryPromotion(category_ids=[ALL_INSPECTED_SELECTION_ID], mode="full"),
    )
    assert all_checked["include_all_inspected"] is True
    assert len(all_checked["items"]) == 1


def test_ai_profile_schema_requires_meaning_and_processing_recommendation():
    required = RESULT_SCHEMA["properties"]["items"]["items"]["required"]
    assert {"purpose", "secondary_category_ids", "importance", "recommended_mode"} <= set(required)


def test_agent_retry_feedback_covers_the_ai_understanding_contract():
    assert "purpose" in AGENT_RETRY_VALIDATION_FEEDBACK
    assert "secondary_category_ids" in AGENT_RETRY_VALIDATION_FEEDBACK
    assert "importance" in AGENT_RETRY_VALIDATION_FEEDBACK
    assert "recommended_mode" in AGENT_RETRY_VALIDATION_FEEDBACK


def test_stale_agent_result_not_used(test_settings, source_root):
    file = source_root / "data.txt"
    file.write_text("甲方乙方约定", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(
        SummaryJobRequest(
            path=str(source_root), provider="external", allow_remote_processing=True,
            allow_restricted_remote_processing=True,
        )
    )["id"]
    wait(jobs, job)
    packet = jobs.packet(job)
    file.write_text("新版本资料", encoding="utf-8")
    jobs.accept(
        job,
        packet["packet_id"],
        {
            "items": [
                {
                    "id": packet["items"][0]["id"],
                    "summary": "旧说明",
                    "evidence": "甲方",
                    "category_id": "finance_contract",
                    "uncertainty": "仅抽样",
                }
            ]
        },
    )
    assert jobs.view(job)["ai_counts"]["stale"] == 1
    assert jobs.view(job)["records"][0]["summary"]["origin"] == "local"


def test_large_manifest_has_no_5000_cap(test_settings, source_root):
    jobs = SummaryJobs(test_settings)
    job = jobs.create(SummaryJobRequest(path=str(source_root)), start=False)["id"]
    jobs._discover_batch(job, [(str(source_root / f"{i}.txt"), False) for i in range(5100)])
    assert jobs.view(job, limit=0)["counts"]["pending"] == 5100
    assert len(jobs.view(job, offset=5050)["records"]) == 50


def test_remote_consent_and_cross_origin_denied(test_settings, source_root):
    jobs = SummaryJobs(test_settings)
    with pytest.raises(ValueError, match="勾选"):
        jobs.create(SummaryJobRequest(path=str(source_root), provider="codex"))
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/foundation/summary-jobs",
            json={"path": str(source_root)},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403


def test_cloud_worker_with_fake_adapter(test_settings, source_root, monkeypatch):
    (source_root / "a.txt").write_text("甲方乙方", encoding="utf-8")

    class FakeAgent:
        thread_id = None

        def __init__(self, *args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def summarize(self, packet, on_thread):
            on_thread("synthetic-thread")
            return {
                "items": [
                    {
                        "id": packet["items"][0]["id"],
                        "summary": "合同",
                        "category_id": "finance_contract",
                        "evidence": "甲方",
                        "uncertainty": "抽样",
                    }
                ]
            }, "synthetic-thread"

    monkeypatch.setattr("pkas.summary_jobs.CodexAgent", FakeAgent)
    monkeypatch.setattr("pkas.summary_agent.codex_binary", lambda _: Path("test.exe"))
    jobs = SummaryJobs(test_settings)
    job = jobs.create(
        SummaryJobRequest(
            path=str(source_root), provider="codex", allow_remote_processing=True,
            allow_restricted_remote_processing=True,
        )
    )["id"]
    result = wait(jobs, job)
    assert result["stage"] == "done", result["message"]
    assert result["ai_counts"]["done"] == 1


def test_cloud_worker_retries_invalid_evidence_once(test_settings, source_root, monkeypatch):
    (source_root / "a.txt").write_text("甲方乙方", encoding="utf-8")

    class RepairingAgent:
        thread_id = None
        calls = 0

        def __init__(self, *args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def summarize(self, packet, on_thread):
            self.calls += 1
            on_thread("repair-thread")
            evidence = "不在样本里" if self.calls == 1 else "甲方"
            if self.calls == 2:
                assert "validation_feedback" in packet
            return {
                "items": [{
                    "id": packet["items"][0]["id"],
                    "summary": "合同",
                    "category_id": "finance_contract",
                    "evidence": evidence,
                    "uncertainty": "抽样",
                }]
            }, "repair-thread"

    monkeypatch.setattr("pkas.summary_jobs.CodexAgent", RepairingAgent)
    monkeypatch.setattr("pkas.summary_agent.codex_binary", lambda _: Path("test.exe"))
    jobs = SummaryJobs(test_settings)
    ident = jobs.create(
        SummaryJobRequest(
            path=str(source_root), provider="codex", allow_remote_processing=True,
            allow_restricted_remote_processing=True,
        )
    )["id"]
    result = wait(jobs, ident)
    assert result["stage"] == "done", result["message"]
    assert result["ai_counts"]["done"] == 1


def test_cloud_worker_keeps_local_result_after_second_invalid_evidence(
    test_settings, source_root, monkeypatch
):
    source = source_root / "a.txt"
    source.write_text("甲方乙方", encoding="utf-8")
    seed_catalog(test_settings, source)

    class InvalidEvidenceAgent:
        thread_id = None

        def __init__(self, *args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def summarize(self, packet, on_thread):
            on_thread("invalid-evidence-thread")
            return {
                "items": [{
                    "id": packet["items"][0]["id"],
                    "summary": "未经验证的模型摘要",
                    "category_id": "finance_contract",
                    "evidence": "不在本地样本中",
                    "uncertainty": "无",
                }]
            }, "invalid-evidence-thread"

    monkeypatch.setattr("pkas.summary_jobs.CodexAgent", InvalidEvidenceAgent)
    monkeypatch.setattr("pkas.summary_agent.codex_binary", lambda _: Path("test.exe"))
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    ident = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch",
            provider="codex",
            allow_remote_processing=True,
            allow_restricted_remote_processing=True,
            catalog_batch_size=1,
        )
    )["id"]
    result = wait(jobs, ident)
    assert result["stage"] == "done", result["message"]
    assert result["state"] == "warning"
    assert result["ai_counts"]["rejected"] == 1
    assert result["records"][0]["summary"]["origin"] == "local"
    assert result["ai_rejected_batches"] == 1


def test_cloud_worker_keeps_local_result_after_second_invalid_item_count(
    test_settings, source_root, monkeypatch
):
    source = source_root / "a.txt"
    source.write_text("甲方乙方", encoding="utf-8")
    seed_catalog(test_settings, source)

    class MissingItemAgent:
        thread_id = None

        def __init__(self, *args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def summarize(self, packet, on_thread):
            on_thread("missing-item-thread")
            return {"items": []}, "missing-item-thread"

    monkeypatch.setattr("pkas.summary_jobs.CodexAgent", MissingItemAgent)
    monkeypatch.setattr("pkas.summary_agent.codex_binary", lambda _: Path("test.exe"))
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    ident = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch",
            provider="codex",
            allow_remote_processing=True,
            allow_restricted_remote_processing=True,
            catalog_batch_size=1,
        )
    )["id"]
    result = wait(jobs, ident)
    assert result["stage"] == "done", result["message"]
    assert result["state"] == "warning"
    assert result["ai_counts"]["rejected"] == 1
    assert result["records"][0]["summary"]["origin"] == "local"


def test_deepseek_worker_uses_same_packet_validation(test_settings, source_root, monkeypatch):
    source = source_root / "a.txt"
    source.write_text("甲方乙方", encoding="utf-8")
    seed_catalog(test_settings, source)

    class FakeGateway:
        def __init__(self, **_kwargs):
            pass

        def complete_json(self, **kwargs):
            packet = kwargs["payload"]
            response = {
                "items": [{
                    "id": packet["items"][0]["id"],
                    "summary": "合同相关资料",
                    "category_id": "finance_contract",
                    "evidence": "甲方",
                    "uncertainty": "仅依据抽样",
                }]
            }
            assert kwargs["task_type"] == "catalog_classification"
            assert kwargs["use_cache"] is False
            assert kwargs["validator"](response) == response
            return SimpleNamespace(content=response)

    monkeypatch.setattr(
        type(test_settings), "deepseek_enabled", property(lambda _settings: True)
    )
    monkeypatch.setattr("pkas.summary_jobs.DeepSeekGateway", FakeGateway)
    jobs = SummaryJobs(test_settings)
    monkeypatch.setattr(jobs, "_safe", lambda path, roots: True)
    created = jobs.create(
        SummaryJobRequest(
            scope="catalog_batch",
            provider="deepseek",
            allow_remote_processing=True,
            allow_restricted_remote_processing=True,
            catalog_batch_size=1,
        )
    )
    assert created["request"]["batch_size"] == 1
    ident = created["id"]
    result = wait(jobs, ident)
    assert result["stage"] == "done", result["message"]
    assert result["state"] == "completed"
    assert result["ai_counts"]["done"] == 1
    assert result["records"][0]["summary"]["origin"] == "agent"


def test_all_local_drives_are_one_resumable_job(test_settings, tmp_path, monkeypatch):
    first = tmp_path / "drive-c"
    second = tmp_path / "drive-e"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr("pkas.summary_jobs.local_fixed_roots", lambda: [first, second])
    jobs = SummaryJobs(test_settings)
    job = jobs.create(
        SummaryJobRequest(scope="all_local_drives", provider="local"), start=False
    )
    assert job["root"] == "所有本地固定磁盘"
    assert job["roots"] == [str(first), str(second)]
    assert job["scope"] == "all_local_drives"
    assert job["pending_directories"] == 2


def test_all_local_drives_discovers_and_reads_each_root(test_settings, tmp_path, monkeypatch):
    # The test simulates two local drives under pytest's AppData temp root.
    # Explicitly authorize that fixture without weakening the production guard.
    monkeypatch.setattr("pkas.directory_summary.EXCLUDED", frozenset())
    monkeypatch.setattr("pkas.directory_summary.user_documents_path", lambda: tmp_path)
    monkeypatch.setattr("pkas.summary_jobs.EXCLUDED", frozenset())
    first = tmp_path / "drive-c"
    second = tmp_path / "drive-e"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_text("甲方乙方", encoding="utf-8")
    (second / "two.txt").write_text("会议纪要", encoding="utf-8")
    monkeypatch.setattr("pkas.summary_jobs.local_fixed_roots", lambda: [first, second])
    jobs = SummaryJobs(test_settings)
    ident = jobs.create(
        SummaryJobRequest(scope="all_local_drives", provider="local")
    )["id"]
    result = wait(jobs, ident)
    assert result["stage"] == "done", result["message"]
    assert result["counts"] == {"inspected": 2}
    assert {row["record"]["relative"] for row in result["records"]} == {
        str(first / "one.txt"),
        str(second / "two.txt"),
    }


def test_completed_categories_can_create_fulltext_intake_without_rescan(
    test_settings, source_root
):
    source = source_root / "contract.txt"
    source.write_text("甲方乙方约定唯一分类提升验收词", encoding="utf-8")
    with TestClient(create_app(test_settings)) as client:
        created = client.post(
            "/api/foundation/summary-jobs", json={"path": str(source_root), "scope": "path"}
        ).json()["data"]
        result = wait(client.app.state.summary_jobs, created["id"])
        category_id = result["records"][0]["record"]["classification"]["category_id"]
        assert result["classification_counts"][0]["count"] == 1

        response = client.post(
            f"/api/foundation/summary-jobs/{created['id']}/promote-preview",
            json={"category_ids": [category_id], "mode": "full"},
        )
        assert response.status_code == 200, response.text
        plan = response.json()["data"]
        assert plan["state"] == "ready"
        assert plan["scanner"] == "saved_classification_manifest"
        assert plan["counts"] == {"pending": 1}
        assert plan["items"][0]["action"] == "full"
        assert plan["items"][0]["content_category_id"] == category_id

        confirmed = client.post(
            f"/api/foundation/intake/{plan['id']}/confirm?confirmed=true"
        )
        assert confirmed.status_code == 200
        wait_service = client.app.state.intake
        wait_service.thread.join(20)
        final = wait_service.read(plan["id"])
        assert final["state"] == "completed"
        assert client.app.state.system.repository.search("唯一分类提升验收词")
