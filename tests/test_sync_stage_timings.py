import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pkas import scheduled_sync_worker as worker
from pkas.run_diagnostics import read_run_receipts
from pkas.scheduled_sync import run_scheduled_sync
from pkas.system import KnowledgeSystem

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "PRIVATE_VALUE_DO_NOT_LOG"


def clock(monkeypatch: pytest.MonkeyPatch, values: list[int]) -> None:
    times = iter(value * 1_000_000 for value in values)
    monkeypatch.setattr("pkas.scheduled_sync.time.perf_counter_ns", lambda: next(times))


def test_stage_timings_are_observed_without_using_wall_clock(knowledge_system, monkeypatch):
    clock(monkeypatch, [1000, 3000, 4000, 7000])
    result = run_scheduled_sync(
        knowledge_system, now_ms=10000, boot_id=1,
        weflow_import_runner=lambda _: {"status": "completed", "imported_messages": 2},
        full_sync_runner=lambda _: {"status": "completed", "selected_roots": 1},
    )
    assert result["timings_ms"] == {"weflow_import": 2000, "local_refresh": 3000}


def test_failed_stage_keeps_observed_time_and_existing_import_counts(knowledge_system, monkeypatch):
    def fail(_):
        raise OSError(PRIVATE)

    clock(monkeypatch, [1000, 3000, 4000, 7000])
    result = run_scheduled_sync(
        knowledge_system, now_ms=10000, boot_id=1,
        weflow_import_runner=lambda _: {"status": "completed", "imported_messages": 2},
        full_sync_runner=fail,
    )
    assert result["status"] == "failed"
    path = worker._write_report(knowledge_system, {"status": "failed", "result": result})
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    assert receipt["counts"]["weflow_imported_messages"] == 2
    assert receipt["counts"]["local_refresh_ms"] == 3000
    assert PRIVATE not in json.dumps(receipt)


def test_not_due_local_refresh_has_no_fabricated_zero_timing(knowledge_system, monkeypatch):
    run_scheduled_sync(
        knowledge_system, now_ms=10000, boot_id=1,
        full_sync_runner=lambda _: {"status": "completed"},
        weflow_import_runner=lambda _: {"status": "disabled"},
    )
    clock(monkeypatch, [1000, 3000, 4000])
    result = run_scheduled_sync(
        knowledge_system, now_ms=11000, boot_id=1,
        full_sync_runner=lambda _: pytest.fail("No scan should run"),
        weflow_import_runner=lambda _: {"status": "disabled"},
    )
    assert result["full_sync_due"] is False
    assert result["timings_ms"] == {"weflow_import": 2000}


@pytest.mark.parametrize("bad", [True, -1, 1.5, "1000", PRIVATE, None, 86_400_001])
def test_receipt_rejects_invalid_or_private_timing_values(tmp_path: Path, bad: object):
    path = worker._write_report_at(tmp_path, {
        "status": "completed", "export_wait_ms": bad, "worker_elapsed_ms": bad,
        "result": {"timings_ms": {"weflow_import": bad, "local_refresh": bad, PRIVATE: bad}},
    })
    output = Path(path).read_text(encoding="utf-8")
    receipt = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert not any(key.endswith("_ms") for key in receipt["counts"])
    assert PRIVATE not in output + json.dumps(receipt)


def test_worker_receipt_carries_export_and_python_stage_timings(tmp_path: Path):
    worker._write_report_at(tmp_path, {
        "status": "failed", "export_wait_ms": 60000, "worker_elapsed_ms": 120000,
        "result": {"weflow_export_status": "error", "timings_ms": {
            "weflow_import": 5000, "local_refresh": 90000,
        }},
    })
    latest = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert {key: value for key, value in latest["counts"].items() if key.endswith("_ms")} == {
        "weflow_export_wait_ms": 60000, "weflow_import_ms": 5000,
        "local_refresh_ms": 90000, "worker_total_ms": 120000,
    }
    assert latest["status"] == "failed"


def test_worker_entry_uses_numeric_timing_contract_in_an_isolated_database(
    knowledge_system, source_root: Path, monkeypatch, capsys,
):
    knowledge_system.sync.register_root(
        name="Synthetic source", root_path=str(source_root), connector_type="local_files",
    )
    monkeypatch.setattr(KnowledgeSystem, "create", classmethod(lambda cls: knowledge_system))
    monkeypatch.setattr(sys, "argv", ["worker", "--local-only", "--weflow-export-wait-ms", "60000"])
    worker.main()
    receipt = read_run_receipts(knowledge_system.settings.data_root)["scheduled-sync"]["latest"]
    assert receipt["status"] == "completed"
    assert receipt["counts"]["weflow_export_wait_ms"] == 60000
    assert receipt["counts"]["worker_total_ms"] >= 0
    assert receipt["counts"]["local_refresh_ms"] >= 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


@pytest.mark.parametrize("bad", [PRIVATE, "-1", "86400001", "1.2"])
def test_bad_cli_timing_never_echoes_private_input(bad: str):
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        worker._duration_argument(bad)
    assert PRIVATE not in str(caught.value)


def frontend(payload: dict) -> dict:
    node = shutil.which("node")
    assert node
    uri = (ROOT / "web/src/sync-timing.ts").as_uri()
    code = f"import {{formatSyncDuration,syncTimingStages}} from {json.dumps(uri)};"
    code += f"const input={json.dumps(payload)};"
    code += "console.log(JSON.stringify({formatted:formatSyncDuration(input.ms),"
    code += "stages:syncTimingStages(input.counts)}));"
    result = subprocess.run(
        [node, "--input-type=module", "-e", code], capture_output=True,
        text=True, encoding="utf-8", check=False, timeout=20,
        env={**os.environ, "TZ": "Asia/Taipei"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("ms,label", [
    (None, "未记录"), (-1, "未记录"), (True, "未记录"), ("1000", "未记录"),
    (0, "不到1秒"), (59999, "59秒"), (61000, "1分 1秒"), (3660000, "1小时 1分"),
])
def test_actual_frontend_formats_only_observed_finite_durations(ms, label):
    assert frontend({"ms": ms})["formatted"] == label


def test_actual_frontend_distinguishes_skips_from_missing_old_metrics():
    assert frontend({"counts": {"weflow_enabled": 1}})["stages"] == []
    stages = frontend({"counts": {
        "weflow_enabled": 0, "full_sync_due": 0, "worker_total_ms": 1,
    }})["stages"]
    assert [stage["value"] for stage in stages] == ["未启用", "未启用", "本次无需刷新"]
    measured = frontend({"counts": {
        "weflow_enabled": 1, "full_sync_due": 1,
        "weflow_export_wait_ms": 60000, "weflow_import_ms": 2000, "local_refresh_ms": 90000,
    }})["stages"]
    assert [stage["value"] for stage in measured] == ["1分", "2秒", "1分 30秒"]


def test_launcher_captures_wait_with_monotonic_stopwatch_and_passes_ms():
    source = (ROOT / "scripts/run_scheduled_sync.ps1").read_text(encoding="utf-8")
    assert "[Diagnostics.Stopwatch]::StartNew()" in source
    assert "$exportWaitWatch.Stop()" in source
    assert "'--weflow-export-wait-ms', [string]$exportWaitWatch.ElapsedMilliseconds" in source
