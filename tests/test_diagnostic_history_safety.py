import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.run_diagnostics import ACTION, MAX_COUNTER, read_run_receipts

PRIVATE = "PRIVATE_VALUE_DO_NOT_LOG"


def receipt(**changes: object) -> dict:
    return {
        "version": 1, "kind": "scheduled-sync", "run_id": uuid.uuid4().hex,
        "recorded_at": "2026-10-03T00:10:00+08:00", "status": "failed",
        "counts": {"weflow_imported_messages": 333}, "issues": [], **changes,
    }


def save(data_root: Path, name: str, payload: object) -> None:
    folder = data_root / "runs/diagnostics/scheduled-sync"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_unknown_fields_and_raw_advice_never_reach_reader(tmp_path: Path) -> None:
    save(tmp_path, "latest.json", receipt(
        raw_message=PRIVATE, result={"private_body": PRIVATE}, prompt=PRIVATE,
        counts={"weflow_imported_messages": 333, "failed_roots": PRIVATE, PRIVATE: 7},
        issues=[{"code": "weflow_schedule_not_due", "count": 1,
                 "action": PRIVATE, "stage": PRIVATE, "error_type": PRIVATE}],
    ))
    result = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert PRIVATE not in json.dumps(result)
    assert result["counts"] == {"weflow_imported_messages": 333}
    assert result["issues"][0]["action"] == ACTION["weflow_schedule_not_due"]
    assert result["issues"][0]["stage"] == "等待消息导出"


def test_runtime_api_sanitizes_the_actual_legacy_history_boundary(test_settings) -> None:
    save(test_settings.data_root, "latest.json", receipt(raw_exception=PRIVATE))
    save(test_settings.data_root, "recent-issues.json", [receipt(raw_chat=PRIVATE)])
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/runtime/overview")
    assert response.status_code == 200
    assert PRIVATE not in response.text
    latest = response.json()["data"]["sync_diagnostics"]["scheduled-sync"]["latest"]
    assert latest["counts"]["weflow_imported_messages"] == 333


@pytest.mark.parametrize("bad", [True, -1, 1.5, MAX_COUNTER + 1, [], {}, PRIVATE, None])
def test_counter_types_and_numeric_bounds_fail_closed(tmp_path: Path, bad: object) -> None:
    save(tmp_path, "latest.json", receipt(counts={"weflow_imported_messages": bad}))
    assert read_run_receipts(tmp_path)["scheduled-sync"]["latest"]["counts"] == {}


@pytest.mark.parametrize("changed", [
    {"version": True}, {"version": 2}, {"kind": "sync"}, {"status": PRIVATE},
    {"status": []}, {"status": {}},
    {"run_id": PRIVATE}, {"recorded_at": PRIVATE},
    {"recorded_at": "2026-10-03T00:10:00"}, {"issues": {}}, {"counts": []},
])
def test_malformed_envelopes_are_not_promoted_to_latest(tmp_path: Path, changed: dict) -> None:
    save(tmp_path, "latest.json", receipt(**changed))
    assert read_run_receipts(tmp_path)["scheduled-sync"]["latest"] is None


def test_latest_is_sorted_by_actual_instant_not_timezone_text(tmp_path: Path) -> None:
    newer = receipt(recorded_at="2026-10-02T23:30:00+00:00", status="completed")
    older = receipt(recorded_at="2026-10-03T00:10:00+08:00", status="failed")
    save(tmp_path, "latest.json", older)
    save(tmp_path, "launcher-latest.json", newer)
    latest = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert latest["run_id"] == newer["run_id"]
    assert latest["recorded_at"] == "2026-10-02T23:30:00.000000+00:00"


def test_same_run_merge_keeps_failure_and_real_import_counts(tmp_path: Path) -> None:
    run_id = uuid.uuid4().hex
    save(tmp_path, "latest.json", receipt(
        run_id=run_id, recorded_at="2026-10-03T00:10:00+08:00", status="failed",
        issues=[{"code": "weflow_schedule_not_due", "count": 1}],
    ))
    save(tmp_path, "launcher-latest.json", receipt(
        run_id=run_id, recorded_at="2026-10-02T16:11:00Z", status="warning", counts={},
        issues=[{"code": "weflow_cleanup_failed", "count": 1}],
    ))
    latest = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert latest["status"] == "failed"
    assert latest["counts"]["weflow_imported_messages"] == 333
    assert {row["code"] for row in latest["issues"]} == {
        "weflow_schedule_not_due", "weflow_cleanup_failed",
    }


def test_oversized_and_broken_history_does_not_hide_valid_latest(tmp_path: Path) -> None:
    save(tmp_path, "latest.json", receipt())
    folder = tmp_path / "runs/diagnostics/scheduled-sync"
    (folder / "recent-issues.json").write_text(" " * (1024 * 1024 + 1), encoding="utf-8")
    (folder / "launcher-recent-issues.json").write_text("{", encoding="utf-8")
    data = read_run_receipts(tmp_path)["scheduled-sync"]
    assert data["latest"]["counts"]["weflow_imported_messages"] == 333
    assert data["recent_issues"] == []
