import json
from pathlib import Path

from pkas.run_diagnostics import issue, read_run_receipts, write_run_receipt
from pkas.scheduled_sync_worker import _write_report


def test_receipts_stay_bounded_and_do_not_persist_raw_paths(tmp_path):
    for _ in range(30):
        write_run_receipt(
            tmp_path, kind="sync", status="warning",
            counts={"failed_roots": 1},
            issues=[issue("root_failed", root_id="syn_test")],
        )
    home = tmp_path / "runs" / "diagnostics" / "sync"
    assert {path.name for path in home.iterdir()} == {"latest.json", "recent-issues.json"}
    data = read_run_receipts(tmp_path)["sync"]
    assert len(data["recent_issues"]) == 20
    assert data["latest"]["issues"][0]["code"] == "root_failed"
    assert "syn_test" in json.dumps(data)


def test_receipt_history_survives_success_without_growing(tmp_path):
    write_run_receipt(
        tmp_path, kind="scheduled-sync", status="failed",
        counts={}, issues=[issue("worker_failed")],
    )
    write_run_receipt(
        tmp_path, kind="scheduled-sync", status="completed",
        counts={"full_sync_due": 1}, issues=[],
    )
    data = read_run_receipts(tmp_path)["scheduled-sync"]
    assert data["latest"]["status"] == "completed"
    assert len(data["recent_issues"]) == 1


def test_scheduled_receipt_never_persists_import_payload(knowledge_system):
    report = {
        "status": "warning",
        "result": {
            "full_sync_due": True,
            "full_sync": {"failed_roots": 0, "warning_roots": 1},
            "weflow_import": {"failed_sessions": 1, "private_message": "secret chat body"},
        },
    }
    path = _write_report(knowledge_system, report)
    content = Path(path).read_text(encoding="utf-8")
    assert "secret chat body" not in content
    assert "weflow_partial" in content
    assert "root_partial" in content


def test_scheduled_receipt_identifies_import_low_disk(knowledge_system):
    path = _write_report(knowledge_system, {
        "status": "failed",
        "result": {
            "full_sync_due": False,
            "weflow_import": {"status": "failed", "error_code": "low_disk_space"},
        },
    })
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    assert receipt["counts"]["low_disk"] == 1
    assert receipt["counts"]["weflow_import_low_disk"] == 1
    assert receipt["counts"]["weflow_import_failed"] == 1
    assert [item["code"] for item in receipt["issues"]] == ["low_disk_space"]


def test_scheduled_receipt_does_not_confuse_skipped_export_with_import(knowledge_system):
    path = _write_report(knowledge_system, {
        "status": "completed",
        "result": {
            "full_sync_due": False,
            "weflow_export_status": "skipped",
            "weflow_import": {
                "status": "completed", "candidate_sessions": 0,
                "imported_messages": 0,
            },
        },
    })
    counts = json.loads(Path(path).read_text(encoding="utf-8"))["counts"]
    assert counts["weflow_export_skipped"] == 1
    assert counts["weflow_imported_messages"] == 0
