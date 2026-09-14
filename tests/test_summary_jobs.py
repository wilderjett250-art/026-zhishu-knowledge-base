import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.summary_jobs import SummaryEdit, SummaryJobRequest, SummaryJobs, SummaryPromotion


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
        stat = source.stat()
        db.execute(
            "INSERT INTO files VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(source.parent), str(source), source.name, str(source.parent), "_root",
                source.name, source.suffix, "document", stat.st_size, stat.st_mtime_ns,
                "active", "test", "now", "now",
            ),
        )


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
    selection = jobs.catalog_promotion_selection(
        SummaryPromotion(category_ids=["finance_contract"], mode="full")
    )
    assert selection["source_summary_job"] == "catalog-classification-ledger"
    assert selection["items"][0]["path"] == str(source)
    with pytest.raises(ValueError, match="没有待分类"):
        jobs.create(SummaryJobRequest(scope="catalog_batch"), start=False)
    source.write_text("甲方乙方约定（已更新）", encoding="utf-8")
    stat = source.stat()
    with sqlite3.connect(test_settings.data_root / "machine-catalog" / "catalog.sqlite") as db:
        db.execute(
            "UPDATE files SET byte_size=?,modified_ns=? WHERE id=1",
            (stat.st_size, stat.st_mtime_ns),
        )
    next_job = jobs.create(SummaryJobRequest(scope="catalog_batch"), start=False)
    assert next_job["catalog_batch"]["reserved"] == 1
    with sqlite3.connect(jobs.catalog_ledger.path) as db:
        assert db.execute("SELECT COUNT(*) FROM history WHERE catalog_file_id=1").fetchone()[0] == 1


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
        SummaryJobRequest(path=str(source_root), provider="external", allow_remote_processing=True)
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


def test_read_error_can_be_retried_without_restarting_all(test_settings, source_root):
    file = source_root / "disappearing.txt"
    file.write_text("甲方乙方", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(SummaryJobRequest(path=str(source_root)), start=False)["id"]
    jobs._discover(job, threading.Event())
    file.unlink()
    jobs.resume(job)
    assert wait(jobs, job)["counts"]["error"] == 1
    file.write_text("甲方乙方", encoding="utf-8")
    jobs.retry_errors(job)
    result = wait(jobs, job)
    assert result["counts"] == {"inspected": 1}
    assert result["stage"] == "done"


def test_external_agent_validates_evidence_and_deduplicates(test_settings, source_root):
    (source_root / "data.txt").write_text("甲方乙方约定", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(
        SummaryJobRequest(path=str(source_root), provider="external", allow_remote_processing=True)
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


def test_stale_agent_result_not_used(test_settings, source_root):
    file = source_root / "data.txt"
    file.write_text("甲方乙方约定", encoding="utf-8")
    jobs = SummaryJobs(test_settings)
    job = jobs.create(
        SummaryJobRequest(path=str(source_root), provider="external", allow_remote_processing=True)
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

        def __init__(self, *args):
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
        SummaryJobRequest(path=str(source_root), provider="codex", allow_remote_processing=True)
    )["id"]
    result = wait(jobs, job)
    assert result["stage"] == "done", result["message"]
    assert result["ai_counts"]["done"] == 1


def test_cloud_worker_retries_invalid_evidence_once(test_settings, source_root, monkeypatch):
    (source_root / "a.txt").write_text("甲方乙方", encoding="utf-8")

    class RepairingAgent:
        thread_id = None
        calls = 0

        def __init__(self, *args):
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
            path=str(source_root), provider="codex", allow_remote_processing=True
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

        def __init__(self, *args):
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

        def __init__(self, *args):
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
