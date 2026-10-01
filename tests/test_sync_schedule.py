"""Missed-day checks use synthetic state, isolated SQLite and mocked Task Scheduler."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pkas import scheduled_sync_worker as worker
from pkas.run_diagnostics import read_run_receipts
from pkas.sync_schedule import (
    daily_sync_decision,
    latest_daily_boundary,
    record_daily_success,
)
from pkas.system import KnowledgeSystem
from pkas.weflow_daily import authorize_daily_import
from tests.test_weflow_daily import EXPORT_WATERMARK_MS, _create_daily_export

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=LOCAL_ZONE)


def _success(data_root: Path, when: datetime, *, weflow: bool = True) -> Path:
    ms = int(when.timestamp() * 1000)
    return record_daily_success(
        data_root, run_id=uuid.uuid4().hex, started_at_ms=ms,
        completed_at_ms=ms + 1000, weflow_checked=weflow,
    )


@pytest.mark.parametrize("uptime,wait", [(0, 3600), (60, 3540), (3599.2, 1)])
def test_missed_day_waits_for_actual_boot_hour(
    tmp_path: Path, uptime: float, wait: int,
) -> None:
    _success(tmp_path, NOW - timedelta(days=1))
    decision = daily_sync_decision(tmp_path, now=NOW, uptime_seconds=uptime)
    assert decision == {"status": "boot-delay", "delay_seconds": wait}


@pytest.mark.parametrize("uptime", [3600, 3601, 4 * 3600])
def test_late_login_catches_up_without_waiting_an_extra_hour(tmp_path: Path, uptime: int) -> None:
    _success(tmp_path, NOW - timedelta(days=3))
    assert daily_sync_decision(tmp_path, now=NOW, uptime_seconds=uptime)["status"] == "due"


def test_a_successful_check_including_zero_messages_is_not_repeated(tmp_path: Path) -> None:
    path = _success(tmp_path, NOW - timedelta(hours=1))
    before = path.read_bytes()
    assert daily_sync_decision(tmp_path, now=NOW, uptime_seconds=10)["status"] == "not-due"
    assert path.read_bytes() == before
    assert sorted(tmp_path.rglob("*.json")) == [path]


def test_before_custom_daily_time_uses_previous_boundary(tmp_path: Path) -> None:
    early = NOW.replace(hour=2)
    assert latest_daily_boundary(early, "03:00") == early.replace(hour=3) - timedelta(days=1)
    _success(tmp_path, early - timedelta(hours=1))
    assert daily_sync_decision(
        tmp_path, now=early, daily_at="03:00", uptime_seconds=3600,
    )["status"] == "not-due"
    assert daily_sync_decision(
        tmp_path, now=early.replace(hour=3), daily_at="03:00", uptime_seconds=3600,
    )["status"] == "due"


def test_a_run_crossing_midnight_does_not_mask_the_next_day(tmp_path: Path) -> None:
    start = NOW.replace(hour=23, minute=59) - timedelta(days=1)
    finish = NOW.replace(hour=0, minute=5)
    record_daily_success(
        tmp_path, run_id=uuid.uuid4().hex,
        started_at_ms=int(start.timestamp() * 1000),
        completed_at_ms=int(finish.timestamp() * 1000), weflow_checked=True,
    )
    assert daily_sync_decision(tmp_path, now=NOW, uptime_seconds=3600)["status"] == "due"


def test_enabling_weflow_does_not_reuse_local_only_success(tmp_path: Path) -> None:
    _success(tmp_path, NOW - timedelta(minutes=1), weflow=False)
    assert daily_sync_decision(tmp_path, now=NOW, uptime_seconds=3600)["status"] == "due"
    assert daily_sync_decision(
        tmp_path, now=NOW, uptime_seconds=1, weflow_required=False,
    )["status"] == "not-due"


def test_clock_rollback_does_not_treat_future_success_as_current(tmp_path: Path) -> None:
    _success(tmp_path, NOW + timedelta(days=1))
    assert daily_sync_decision(tmp_path, now=NOW, uptime_seconds=3600)["status"] == "due"


@pytest.mark.parametrize("payload", ["{", "[]", '{"version":0}', "x" * 4097])
def test_corrupt_state_fails_closed_without_rewriting_it(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "config" / "nightly-sync.json"
    path.parent.mkdir()
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        daily_sync_decision(tmp_path, now=NOW, uptime_seconds=3600)
    assert path.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("uptime", [-1, float("nan"), float("inf")])
def test_invalid_uptime_cannot_bypass_the_boot_guard(tmp_path: Path, uptime: float) -> None:
    with pytest.raises(ValueError):
        daily_sync_decision(tmp_path, now=NOW, uptime_seconds=uptime)


def test_failed_marker_replace_preserves_previous_success(tmp_path: Path, monkeypatch) -> None:
    path = _success(tmp_path, NOW - timedelta(days=1))
    before = path.read_bytes()

    def fail(*_args):
        raise PermissionError("PRIVATE_INFO_MUST_NOT_BE_LOGGED")

    monkeypatch.setattr("pkas.sync_schedule.os.replace", fail)
    with pytest.raises(PermissionError):
        _success(tmp_path, NOW)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("status", ["failed", "warning"])
def test_failed_or_partial_worker_never_marks_today_success(
    knowledge_system: KnowledgeSystem, monkeypatch, status: str,
) -> None:
    root = knowledge_system.settings.data_root
    monkeypatch.setenv("PKAS_DATA_ROOT", str(root))
    monkeypatch.setattr(sys, "argv", ["worker", "--record-daily-check"])
    monkeypatch.setattr(KnowledgeSystem, "create", lambda: knowledge_system)
    monkeypatch.setattr("pkas.scheduled_sync.run_scheduled_sync", lambda *args, **kwargs: {
        "status": status, "weflow_export_status": "error" if status == "failed" else "ready",
        "weflow_import": {"status": status, "failed_sessions": 1},
    })
    with pytest.raises(SystemExit):
        worker.main()
    assert not (root / "config" / "nightly-sync.json").exists()


def test_zero_new_messages_records_a_genuine_completed_check(
    knowledge_system: KnowledgeSystem, monkeypatch,
) -> None:
    root = knowledge_system.settings.data_root
    monkeypatch.setenv("PKAS_DATA_ROOT", str(root))
    monkeypatch.setattr(sys, "argv", ["worker", "--record-daily-check"])
    monkeypatch.setattr(KnowledgeSystem, "create", lambda: knowledge_system)
    monkeypatch.setattr("pkas.scheduled_sync.run_scheduled_sync", lambda *args, **kwargs: {
        "status": "deferred", "weflow_export_status": "skipped",
        "weflow_import": {"status": "completed", "failed_sessions": 0, "imported_messages": 0},
    })
    worker.main()
    state = json.loads((root / "config" / "nightly-sync.json").read_text(encoding="utf-8"))
    assert state["weflow_checked"] is True
    assert read_run_receipts(root)["scheduled-sync"]["latest"]["status"] == "deferred"


def test_schedule_marker_failure_is_reported_without_losing_import_counts(
    knowledge_system: KnowledgeSystem, monkeypatch,
) -> None:
    root = knowledge_system.settings.data_root
    monkeypatch.setenv("PKAS_DATA_ROOT", str(root))
    monkeypatch.setattr(sys, "argv", ["worker", "--record-daily-check"])
    monkeypatch.setattr(KnowledgeSystem, "create", lambda: knowledge_system)
    monkeypatch.setattr("pkas.scheduled_sync.run_scheduled_sync", lambda *args, **kwargs: {
        "status": "completed", "weflow_export_status": "ready",
        "weflow_import": {"status": "completed", "failed_sessions": 0, "imported_messages": 3},
    })

    def fail(*_args, **_kwargs):
        raise OSError("PRIVATE_INFO_MUST_NOT_BE_LOGGED")

    monkeypatch.setattr("pkas.sync_schedule.record_daily_success", fail)
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == 1
    receipt = read_run_receipts(root)["scheduled-sync"]["latest"]
    assert receipt["counts"]["weflow_imported_messages"] == 3
    assert any(row["code"] == "sync_state_failed" for row in receipt["issues"])
    assert not (root / "config" / "nightly-sync.json").exists()


def test_real_worker_subprocess_imports_synthetic_messages_and_deduplicates(
    knowledge_system: KnowledgeSystem, source_root: Path,
) -> None:
    records, _ = _create_daily_export(source_root, export_time=EXPORT_WATERMARK_MS + 200)
    authorize_daily_import(
        knowledge_system, records_path=str(records), authorized_at_ms=EXPORT_WATERMARK_MS,
    )
    docs = source_root / "notes"
    docs.mkdir()
    (docs / "sync.md").write_text("Synthetic sync source, not personal data.", encoding="utf-8")
    knowledge_system.sync.register_root(
        name="synthetic-docs", root_path=str(docs), connector_type="local_files", sync_mode="index",
    )
    root = knowledge_system.settings.data_root
    env = os.environ.copy()
    env.update({
        "PKAS_PROJECT_ROOT": str(knowledge_system.settings.project_root),
        "PKAS_DATA_ROOT": str(root),
        "PKAS_INTEGRATION_HOME": str(source_root / "integration"),
        "PKAS_SYNC_RUN_ID": uuid.uuid4().hex,
    })
    command = [sys.executable, "-m", "pkas.scheduled_sync_worker", "--record-daily-check"]
    first = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stdout + first.stderr
    receipt = read_run_receipts(root)["scheduled-sync"]["latest"]
    assert receipt["counts"]["weflow_imported_messages"] == 1
    assert json.loads((root / "config" / "nightly-sync.json").read_text())["weflow_checked"]
    env["PKAS_SYNC_RUN_ID"] = uuid.uuid4().hex
    second = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stdout + second.stderr
    second_receipt = read_run_receipts(root)["scheduled-sync"]["latest"]
    assert second_receipt["counts"]["weflow_imported_messages"] == 0
    with sqlite3.connect(knowledge_system.settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM customer_messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM customer_messages_fts").fetchone()[0] == 1


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell required")
def test_powershell_arms_one_fixed_name_task_at_remaining_boot_delay(tmp_path: Path) -> None:
    # Every Task Scheduler command is replaced; no real task is created here.
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "windows_autostart_policy.json").write_text(
        json.dumps({"schema_version": 1, "autostart_enabled": True}), encoding="utf-8",
    )
    script = r'''
$ErrorActionPreference = 'Stop'
$root = 'PROJECT_PATH'
$data = 'DATA_PATH'
$main = [pscustomobject]@{
    Actions = @([pscustomobject]@{
        WorkingDirectory=$root
        Arguments='-File "RUNNER_PATH" -Scheduled'
    })
    Principal = [pscustomobject]@{LogonType='Interactive'}
}
function Get-ScheduledTask {param($TaskName) if($TaskName -eq 'Test-Sync'){$main}}
function New-ScheduledTaskTrigger {
    param([switch]$Once,$At)
    [pscustomobject]@{Once=[bool]$Once;At=$At}
}
function New-ScheduledTaskSettingsSet { [pscustomobject]@{Synthetic=$true} }
function New-ScheduledTask {
    param($Action,$Trigger,$Principal,$Settings,$Description)
    [pscustomobject]@{Actions=$Action;Trigger=$Trigger;Principal=$Principal}
}
function Register-ScheduledTask {
    param($TaskName,$InputObject,[switch]$Force)
    $script:name=$TaskName
    $script:created=$InputObject
}
. 'ARM_PATH'
$before = Get-Date
Register-PkasSyncCatchUp -ProjectRoot $root -DataRoot $data `
    -TaskName 'Test-Sync' -DelaySeconds 120
if($script:name -ne 'Test-Sync-CatchUp'){throw 'Wrong catch-up name'}
if(-not $script:created.Trigger.Once){throw 'Not one-shot'}
$wait=($script:created.Trigger.At-$before).TotalSeconds
if($wait -lt 120 -or $wait -gt 130){throw 'Not the remaining boot delay'}
if($script:created.Actions[0].Arguments -ne $main.Actions[0].Arguments){
    throw 'Action scope changed'
}
Write-Output 'catchup-ok'
'''
    script = script.replace("ARM_PATH", str(ROOT / "scripts" / "arm_sync_catchup.ps1"))
    script = script.replace("PROJECT_PATH", str(ROOT)).replace("DATA_PATH", str(tmp_path))
    script = script.replace("RUNNER_PATH", str(ROOT / "scripts" / "run_scheduled_sync.ps1"))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "catchup-ok" in result.stdout


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell required")
def test_real_runner_skips_completed_day_without_initializing_a_database(tmp_path: Path) -> None:
    now = datetime.now().astimezone()
    _success(tmp_path, now - timedelta(seconds=2), weflow=False)
    (tmp_path / "config" / "windows_autostart_policy.json").write_text(
        json.dumps({"schema_version": 1, "autostart_enabled": True}), encoding="utf-8",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "scripts" / "run_scheduled_sync.ps1"),
         "-ProjectRoot", str(ROOT), "-DataRoot", str(tmp_path), "-Scheduled", "-LocalOnly"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "not-due"
    assert not (tmp_path / "index" / "pkas.sqlite").exists()
    assert not (tmp_path / "runs").exists()


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell required")
def test_main_and_catchup_share_mutex_and_do_not_start_a_second_worker(tmp_path: Path) -> None:
    script = r'''
$ErrorActionPreference = 'Stop'
$root = 'PROJECT_PATH'
$data = 'DATA_PATH'
$key=$root.ToLowerInvariant()+'|'+$data.ToLowerInvariant()
$sha=[Security.Cryptography.SHA256]::Create()
$hash=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key)))
$sha.Dispose()
$mutex=[Threading.Mutex]::new($false,('Local\PKAS-Sync-'+$hash.Replace('-','')))
$owned=$mutex.WaitOne(0)
try {
    if(-not $owned){throw 'Test mutex unavailable'}
    & POWERSHELL_PATH -NoProfile -NonInteractive -File 'RUNNER_PATH' `
        -ProjectRoot $root -DataRoot $data -LocalOnly
    if($LASTEXITCODE -ne 0){throw 'Concurrent runner did not exit cleanly'}
} finally {
    if($owned){$mutex.ReleaseMutex()}
    $mutex.Dispose()
}
'''
    script = script.replace("PROJECT_PATH", str(ROOT)).replace("DATA_PATH", str(tmp_path))
    script = script.replace("RUNNER_PATH", str(ROOT / "scripts" / "run_scheduled_sync.ps1"))
    ps_path = shutil.which("powershell.exe")
    script = script.replace("POWERSHELL_PATH", f"'{ps_path}'")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "already_running"
    assert not list(tmp_path.iterdir())
