from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from pkas.run_diagnostics import issue
from pkas.scheduled_sync_worker import EXPORT_FAILURE_CODES, _write_report_at

ROOT = Path(__file__).resolve().parents[1]
PS5 = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
          / "System32/WindowsPowerShell/v1.0/powershell.exe")
DAY = 86400000
MIDNIGHT = int(datetime.fromisoformat("2026-10-02T00:00:00+08:00").timestamp() * 1000)


def quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def ps(command: str) -> dict:
    result = subprocess.run(
        [PS5, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def observe(tmp_path: Path, task: dict, *, now: int = MIDNIGHT + 3600000,
            boundary: int = MIDNIGHT) -> dict:
    config = tmp_path / "WeFlow-config.json"
    root = tmp_path / "PKAS-WeFlow-Exports"
    config.write_text(json.dumps({
        "dbPath": "synthetic-db", "myWxid": "wxid_demo",
        "exportAutomationTaskMap": {"synthetic-db::wxid_demo": {"tasks": [{
            "id": "pkas-weflow-daily-v1", "enabled": True, "outputDir": str(root),
            "schedule": {"type": "interval", "intervalDays": 1, "intervalHours": 0},
            **task,
        }]}},
    }), encoding="utf-8")
    return ps("$ErrorActionPreference='Stop'; . "
              + quote(ROOT / "scripts/weflow_export_status.ps1")
              + "; Get-PkasWeFlowExportState -ConfigPath " + quote(config)
              + " -ExportRoot " + quote(root) + f" -DayStartMs {boundary} -NowMs {now}"
              + " | ConvertTo-Json -Compress")


@pytest.mark.parametrize("status", ["success", "skipped", "error"])
def test_today_terminal_export_status_is_retained(tmp_path: Path, status: str) -> None:
    result = observe(tmp_path, {"runState": {
        "lastTriggeredAt": MIDNIGHT + 1000, "lastRunStatus": status,
        "lastError": "PRIVATE_VALUE_DO_NOT_LOG",
    }})
    assert result["Status"] == status
    assert "PRIVATE_VALUE_DO_NOT_LOG" not in json.dumps(result)
    assert result["ErrorCode"] == ("export_failed" if status == "error" else "")


def test_a_prior_success_is_pending_and_exposes_its_next_due_time(tmp_path: Path) -> None:
    last = MIDNIGHT - 6 * 3600000
    result = observe(tmp_path, {"runState": {"lastTriggeredAt": last, "lastRunStatus": "success"}})
    assert result["Status"] == "pending"
    assert result["NextTriggerAt"] == last + DAY
    assert result["PendingKind"] == "schedule"


@pytest.mark.parametrize("status,delay", [("error", 1800000), ("running", 7200000)])
def test_prior_failures_use_retry_deadlines_not_24_hours(
    tmp_path: Path, status: str, delay: int,
) -> None:
    last = MIDNIGHT - 1000
    result = observe(tmp_path, {"runState": {"lastTriggeredAt": last, "lastRunStatus": status}})
    assert result["Status"] == "pending"
    assert result["NextTriggerAt"] == last + delay
    assert result["PendingKind"] == ("retry" if status == "error" else "running")


def test_cross_midnight_wait_uses_the_frozen_invocation_boundary(tmp_path: Path) -> None:
    last = MIDNIGHT - 30000
    result = observe(tmp_path, {"runState": {"lastTriggeredAt": last, "lastRunStatus": "success"}},
                     now=MIDNIGHT + 30000, boundary=MIDNIGHT - DAY)
    assert result["Status"] == "success"


@pytest.mark.parametrize("enabled", [False, "false", "true", 1, None])
def test_task_enablement_requires_a_real_boolean(tmp_path: Path, enabled: object) -> None:
    assert observe(tmp_path, {"enabled": enabled})["ErrorCode"] == "weflow_task_mismatch"


def test_changed_output_directory_fails_closed(tmp_path: Path) -> None:
    result = observe(tmp_path, {"outputDir": str(tmp_path / "outside")})
    assert result["ErrorCode"] == "weflow_task_mismatch"


def test_partial_export_cannot_masquerade_as_success(tmp_path: Path) -> None:
    result = observe(tmp_path, {"runState": {
        "lastTriggeredAt": MIDNIGHT + 1000, "lastRunStatus": "success",
        "lastFailedSessionCount": 2,
    }})
    assert result["Status"] == "error"
    assert result["ErrorCode"] == "some_sessions_failed"


@pytest.mark.parametrize("status,next_due,exited,unreadable_since,expected", [
    ("pending", MIDNIGHT + 3 * 3600000, False, 0, "weflow_schedule_not_due"),
    ("pending", MIDNIGHT + 3600000, False, 0, ""),
    ("pending", 0, True, 0, "weflow_process_exited"),
    ("success", MIDNIGHT + 5 * DAY, True, 0, ""),
    ("pending", 0, False, MIDNIGHT + 1000, "weflow_config_unreadable"),
    ("pending", 0, False, MIDNIGHT + 119000, ""),
])
def test_bounded_wait_decision_is_not_a_sleep_or_false_success(
    status: str, next_due: int, exited: bool, unreadable_since: int, expected: str,
) -> None:
    code = "weflow_config_unreadable" if unreadable_since else ""
    command = "$ErrorActionPreference='Stop'; . " + quote(ROOT / "scripts/weflow_export_status.ps1")
    command += (f"; $state=[pscustomobject]@{{Status='{status}';NextTriggerAt={next_due};"
                f"PendingKind='schedule';ErrorCode='{code}'}}; "
                "Resolve-PkasWeFlowPendingWait -Observed $state"
                f" -NowMs {MIDNIGHT + 121000} -DeadlineMs {MIDNIGHT + 2 * 3600000}"
                f" -WaitStartedMs {MIDNIGHT} -ConfigUnreadableSinceMs {unreadable_since}"
                f" -LauncherExited ${str(exited).lower()}"
                " | ConvertTo-Json -Compress")
    result = ps(command)
    assert result["Stop"] is bool(expected)
    assert result["ErrorCode"] == expected


@pytest.mark.parametrize("kind,elapsed,stop", [
    ("schedule", 59000, False), ("schedule", 60000, True),
    ("running", 60000, False), ("retry", 60000, False),
])
def test_startup_grace_and_active_export_do_not_fail_early(
    kind: str, elapsed: int, stop: bool,
) -> None:
    command = "$ErrorActionPreference='Stop'; . " + quote(ROOT / "scripts/weflow_export_status.ps1")
    command += (f"; $state=[pscustomobject]@{{Status='pending';PendingKind='{kind}';"
                f"NextTriggerAt={MIDNIGHT + DAY};ErrorCode=''}}; "
                "Resolve-PkasWeFlowPendingWait -Observed $state"
                f" -NowMs {MIDNIGHT + elapsed} -WaitStartedMs {MIDNIGHT}"
                f" -DeadlineMs {MIDNIGHT + 2 * 3600000} | ConvertTo-Json -Compress")
    result = ps(command)
    assert result["Stop"] is stop


def node_alignment(existing: dict, *, now: int = MIDNIGHT + 1000) -> dict | None:
    node = shutil.which("node")
    assert node
    module_uri = (ROOT / "scripts/weflow_schedule.mjs").as_uri()
    code = f"import {{alignManagedDailyRun}} from {json.dumps(module_uri)};"
    code += f"console.log(JSON.stringify(alignManagedDailyRun({json.dumps(existing)}, {now})));"
    result = subprocess.run([node, "--input-type=module", "-e", code], capture_output=True,
                            text=True, check=False, timeout=15,
                            env={**os.environ, "TZ": "Asia/Taipei"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_self_launch_aligns_a_drifted_prior_run_and_preserves_success_cursor() -> None:
    success = MIDNIGHT - 5 * 3600000
    result = node_alignment({"runState": {
        "lastTriggeredAt": MIDNIGHT - 6 * 3600000, "lastRunStatus": "success",
        "lastSuccessAt": success, "lastFinishedAt": success, "successCount": 9,
        "lastScheduleKey": "old-key",
    }})
    assert result is not None
    assert result["firstTriggerAt"] == MIDNIGHT
    assert result["runState"]["lastSuccessAt"] == success
    assert result["runState"]["successCount"] == 9
    assert "lastTriggeredAt" not in result["runState"]
    assert "lastScheduleKey" not in result["runState"]


@pytest.mark.parametrize("state", [
    {"lastTriggeredAt": MIDNIGHT + 1, "lastRunStatus": "success"},
    {"lastTriggeredAt": MIDNIGHT + 1, "lastRunStatus": "skipped"},
    {"lastTriggeredAt": MIDNIGHT - 1000, "lastRunStatus": "running"},
])
def test_current_or_running_export_is_not_reset(state: dict) -> None:
    assert node_alignment({"runState": state}) is None


def test_a_new_self_launch_does_not_wait_for_a_future_installer_anchor() -> None:
    result = node_alignment({"schedule": {"firstTriggerAt": MIDNIGHT + DAY}})
    assert result is not None and result["firstTriggerAt"] == MIDNIGHT


@pytest.mark.parametrize("status,last,aligned", [
    ("success", MIDNIGHT - 3600000, True),
    ("success", MIDNIGHT + 1, False),
    ("running", MIDNIGHT - 3600000, False),
    ("error", MIDNIGHT + 1, False),
])
def test_configuration_entry_uses_alignment_without_advancing_success_cursor(
    tmp_path: Path, status: str, last: int, aligned: bool,
) -> None:
    """Run the real entry against a synthetic store, never the local account."""
    node = shutil.which("node")
    assert node
    fake_root = tmp_path / "weflow"
    entry = fake_root / "node_modules/electron-store/index.js"
    entry.parent.mkdir(parents=True)
    (entry.parent / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    entry.write_text(
        "import fs from 'node:fs'; import path from 'node:path';"
        "export default class Store { constructor(o) {"
        "this.file=path.join(o.cwd,'store.json');"
        "this.data=JSON.parse(fs.readFileSync(this.file,'utf8')); }"
        "get(k,d) { return this.data[k] ?? d; }"
        "set(k,v) { this.data[k]=v; fs.writeFileSync(this.file,JSON.stringify(this.data)); } }",
        encoding="utf-8",
    )
    store_dir = tmp_path / "config"
    store_dir.mkdir()
    store_file = store_dir / "store.json"
    success = MIDNIGHT - DAY
    marker = "PRIVATE_VALUE_DO_NOT_LOG"
    store_file.write_text(json.dumps({
        "privateSyntheticMarker": marker,
        "exportSessionMessageCountCacheMap": {"default": {"counts": {"demo": 1}}},
        "contactsListCacheMap": {"default": {"contacts": [{
            "type": "friend", "username": "demo", "displayName": "Synthetic contact",
        }]}},
        "exportAutomationTaskMap": {"default": {"tasks": [{
            "id": "pkas-weflow-daily-v1", "createdAt": MIDNIGHT - 2 * DAY,
            "schedule": {"firstTriggerAt": MIDNIGHT - DAY},
            "runState": {"lastTriggeredAt": last, "lastRunStatus": status,
                         "lastSuccessAt": success, "successCount": 3},
        }]}},
    }), encoding="utf-8")
    clock = tmp_path / "clock.mjs"
    clock.write_text(f"Date.now=()=>{MIDNIGHT + 3600000};", encoding="utf-8")
    output_root = tmp_path / "PKAS-WeFlow-Exports"
    env = {**os.environ, "TZ": "Asia/Taipei", "WEFLOW_ROOT": str(fake_root),
           "WEFLOW_CONFIG_DIR": str(store_dir), "PKAS_WEFLOW_EXPORT_ROOT": str(output_root),
           "PKAS_WEFLOW_FORCE_DUE": "1", "WEFLOW_DAILY_AT": "00:00",
           "WEFLOW_RESET_DAILY_ANCHOR": "0"}
    result = subprocess.run(
        [node, "--import", clock.as_uri(), str(ROOT / "scripts/configure_weflow_daily.mjs")],
        env=env, capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert marker not in result.stdout + result.stderr
    assert json.loads(result.stdout)["invocation_aligned"] is aligned
    stored = json.loads(store_file.read_text(encoding="utf-8"))
    task = stored["exportAutomationTaskMap"]["default"]["tasks"][0]
    assert stored["privateSyntheticMarker"] == marker
    assert task["runState"]["lastSuccessAt"] == success
    assert task["runState"]["successCount"] == 3
    assert task["template"]["optionTemplate"]["exportMedia"] is False
    if aligned:
        assert task["schedule"]["firstTriggerAt"] == MIDNIGHT
        assert "lastTriggeredAt" not in task["runState"]
    else:
        assert task["runState"]["lastTriggeredAt"] == last


@pytest.mark.parametrize("bad_content", ["{", "null", "[]"])
def test_unreadable_config_returns_only_a_fixed_code(tmp_path: Path, bad_content: str) -> None:
    config = tmp_path / "config.json"
    config.write_text(bad_content, encoding="utf-8")
    result = ps("$ErrorActionPreference='Stop'; . "
                + quote(ROOT / "scripts/weflow_export_status.ps1")
                + "; Get-PkasWeFlowExportState -ConfigPath " + quote(config)
                + " -ExportRoot " + quote(tmp_path / "PKAS-WeFlow-Exports")
                + f" -DayStartMs {MIDNIGHT} -NowMs {MIDNIGHT + 1000}"
                + " | ConvertTo-Json -Compress")
    assert result["ErrorCode"] == "weflow_config_unreadable"
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize("code", ["weflow_schedule_not_due", "weflow_process_exited"])
def test_export_diagnostics_preserve_real_imports_and_remain_failed(
    tmp_path: Path, code: str,
) -> None:
    assert code in EXPORT_FAILURE_CODES
    assert issue(code)["stage"] == "等待消息导出"
    output = _write_report_at(tmp_path, {
        "status": "failed", "export_failure_code": code,
        "result": {"weflow_export_status": "error", "weflow_import": {
            "status": "completed", "candidate_sessions": 1, "imported_messages": 2,
        }},
    })
    receipt = json.loads(Path(output).read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["counts"]["weflow_imported_messages"] == 2
    assert receipt["issues"][0]["code"] == code
