"""Isolated fault injection: never start WeFlow or touch real chat/database data."""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from pkas import scheduled_sync_worker as worker
from pkas.run_diagnostics import ACTION, issue, read_run_receipts, write_run_receipt
from pkas.scheduled_sync import run_scheduled_sync
from pkas.system import KnowledgeSystem
from pkas.weflow_daily import authorize_daily_import, run_daily_import
from tests.test_weflow_daily import EXPORT_WATERMARK_MS, _create_daily_export

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TEXT = "PRIVATE_CHAT_OR_SECRET_MUST_NOT_BE_LOGGED"


def _run_worker(monkeypatch: pytest.MonkeyPatch, data_root: Path, *args: str) -> None:
    monkeypatch.setenv("PKAS_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("PKAS_DATA_ROOT", str(data_root))
    monkeypatch.setenv("PKAS_SYNC_RUN_ID", uuid.uuid4().hex)
    monkeypatch.setattr(sys, "argv", ["scheduled-sync", *args])


def _authorized_export(knowledge_system: KnowledgeSystem, source_root: Path) -> Path:
    records, xlsx = _create_daily_export(
        source_root,
        export_time=EXPORT_WATERMARK_MS + 200,
    )
    authorize_daily_import(
        knowledge_system, records_path=str(records), authorized_at_ms=EXPORT_WATERMARK_MS
    )
    return xlsx


def _watermark(knowledge_system: KnowledgeSystem) -> int:
    path = knowledge_system.settings.data_root / "config" / "weflow-daily-import.json"
    return json.loads(path.read_text(encoding="utf-8"))["last_completed_export_time_ms"]


@pytest.mark.parametrize("exc", [PermissionError, ValueError, RuntimeError])
def test_initialization_failures_have_safe_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    exc,
) -> None:
    _run_worker(monkeypatch, tmp_path, "--local-only")

    def fail(*_args, **_kwargs):
        raise exc(PRIVATE_TEXT)

    monkeypatch.setattr(KnowledgeSystem, "create", fail)
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == 1
    receipt = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert receipt["issues"][0]["code"] == "worker_initialization"
    assert receipt["issues"][0]["error_type"] == exc.__name__
    assert receipt["issues"][0]["stage"] == "同步初始化"
    assert PRIVATE_TEXT not in json.dumps(receipt) + capsys.readouterr().out


@pytest.mark.parametrize("payload", ["{bad JSON", '{"version":1,"enabled":"true"}', "[]"])
def test_invalid_authorization_is_reported_without_opening_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    _run_worker(monkeypatch, tmp_path, "--check-daily-authorization")
    config = tmp_path / "config"
    config.mkdir()
    (config / "weflow-daily-import.json").write_text(payload, encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        pytest.fail("authorization check must not initialize a database")

    monkeypatch.setattr(KnowledgeSystem, "create", forbidden)
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == 1
    latest = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert latest["issues"][0]["code"] == "weflow_authorization_failed"
    assert not (tmp_path / "index").exists()


def test_authorization_check_and_its_diagnostics_need_no_third_party_dependencies(
    tmp_path: Path,
) -> None:
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PKAS_PROJECT_ROOT": str(ROOT),
        "PKAS_DATA_ROOT": str(tmp_path),
    }
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "pkas.scheduled_sync_worker", "--check-daily-authorization"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 2, completed.stderr
    assert json.loads(completed.stdout)["status"] == "disabled"
    assert not (tmp_path / "index").exists()
    config = tmp_path / "config"
    config.mkdir()
    (config / "weflow-daily-import.json").write_text("{invalid", encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, "-S", "-m", "pkas.scheduled_sync_worker", "--check-daily-authorization"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert failed.returncode == 1
    assert read_run_receipts(tmp_path)["scheduled-sync"]["latest"]["status"] == "failed"


def test_missing_python_is_reported_by_powershell_without_any_python_dependency(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None
    replica = tmp_path / "no-runtime"
    replica.mkdir()
    data_root = tmp_path / "data"
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "run_scheduled_sync.ps1"),
            "-ProjectRoot",
            str(replica),
            "-DataRoot",
            str(data_root),
            "-LocalOnly",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    latest = read_run_receipts(data_root)["scheduled-sync"]["latest"]
    assert latest["issues"][0]["code"] == "worker_runtime_missing"
    assert latest["issues"][0]["stage"] == "环境检查"
    assert str(replica) not in completed.stdout + completed.stderr
    assert {path.name for path in (data_root / "runs/diagnostics/scheduled-sync").iterdir()} == {
        "launcher-latest.json",
        "launcher-recent-issues.json",
    }


def test_launcher_history_is_bounded_and_codes_match_python_catalog(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None
    helper = str(ROOT / "scripts" / "write_sync_diagnostic.ps1").replace("'", "''")
    source = Path(helper).read_text(encoding="utf-8")
    code_block = source.split("$actions = @{", 1)[1].split("\n    }", 1)[0]
    codes = re.findall(r"^\s+(\w+) =", code_block, re.MULTILINE)
    assert codes and all(code in ACTION for code in codes)
    target = str(tmp_path).replace("'", "''")
    command = (
        f". '{helper}'; 1..25 | ForEach-Object {{ "
        f"Write-PkasLauncherDiagnostic -DataRoot '{target}' "
        "-RunId ([Guid]::NewGuid().ToString('N')) -Code worker_runtime_missing }"
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = read_run_receipts(tmp_path)["scheduled-sync"]
    assert len(receipt["recent_issues"]) == 20
    home = tmp_path / "runs/diagnostics/scheduled-sync"
    assert sum(path.stat().st_size for path in home.iterdir()) < 32 * 1024


def test_disabled_weflow_does_not_require_or_start_export_environment(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None
    data_root = tmp_path / "data"
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "run_scheduled_sync.ps1"),
            "-ProjectRoot",
            str(ROOT),
            "-DataRoot",
            str(data_root),
            "-WeFlowRoot",
            str(tmp_path / "nonexistent-weflow"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    latest = read_run_receipts(data_root)["scheduled-sync"]["latest"]
    assert latest["status"] == "warning"
    assert latest["counts"]["weflow_enabled"] == 0
    assert [item["code"] for item in latest["issues"]] == ["no_registered_roots"]


def test_later_cleanup_warning_cannot_erase_launcher_failure(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None
    helper = str(ROOT / "scripts" / "write_sync_diagnostic.ps1").replace("'", "''")
    target = str(tmp_path).replace("'", "''")
    run_id = uuid.uuid4().hex
    command = (
        f". '{helper}'; Write-PkasLauncherDiagnostic -DataRoot '{target}' "
        f"-RunId '{run_id}' -Code weflow_launch_failed; "
        f"Write-PkasLauncherDiagnostic -DataRoot '{target}' -RunId '{run_id}' "
        "-Code weflow_cleanup_failed -Status warning"
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    diagnostics = read_run_receipts(tmp_path)["scheduled-sync"]
    assert diagnostics["latest"]["status"] == "failed"
    assert {item["code"] for item in diagnostics["latest"]["issues"]} == {
        "weflow_launch_failed",
        "weflow_cleanup_failed",
    }
    assert len(diagnostics["recent_issues"]) == 1


def test_missing_worker_dependency_can_still_write_initialization_failure(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "pkas.scheduled_sync_worker", "--local-only"],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PKAS_PROJECT_ROOT": str(ROOT),
            "PKAS_DATA_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 1
    latest = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert latest["issues"][0]["code"] == "worker_initialization"
    assert latest["issues"][0]["error_type"] == "ModuleNotFoundError"
    assert not (tmp_path / "index").exists()


def test_unknown_exception_type_is_not_persisted(tmp_path: Path) -> None:
    path = write_run_receipt(
        tmp_path,
        kind="scheduled-sync",
        status="failed",
        counts={},
        issues=[issue("weflow_import_failed", error_type=PRIVATE_TEXT)],
    )
    assert PRIVATE_TEXT not in path.read_text(encoding="utf-8")


def test_malformed_diagnostic_details_do_not_break_reader(tmp_path: Path) -> None:
    path = write_run_receipt(tmp_path, kind="scheduled-sync", status="failed", counts={}, issues=[])
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["issues"] = [
        {"code": [], "count": 1},
        {"code": "weflow_import_io", "count": PRIVATE_TEXT},
        {"code": "weflow_import_failed", "count": 1, "error_type": [], "root_id": {}},
    ]
    path.write_text(json.dumps(receipt), encoding="utf-8")
    latest = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert [item["code"] for item in latest["issues"]] == ["weflow_import_failed"]
    assert "root_id" not in latest["issues"][0] and "error_type" not in latest["issues"][0]


@pytest.mark.parametrize("code", worker.EXPORT_FAILURE_CODES)
def test_export_failures_preserve_specific_stage_and_advice(
    knowledge_system: KnowledgeSystem,
    code: str,
) -> None:
    path = worker._write_report(
        knowledge_system,
        {
            "status": "failed",
            "export_failure_code": code,
            "result": {"weflow_export_status": "error"},
        },
    )
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    assert receipt["issues"][0]["code"] == code
    assert receipt["issues"][0]["stage"]
    assert receipt["issues"][0]["action"]


def test_catalog_failure_does_not_advance_watermark_or_leak_exception(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorized_export(knowledge_system, source_root)

    def fail(**_kwargs):
        raise PermissionError(PRIVATE_TEXT)

    monkeypatch.setattr(knowledge_system.weflow, "discover_exports", fail)
    result = run_daily_import(knowledge_system)
    assert result["status"] == "failed"
    assert result["error_code"] == "weflow_catalog_failed"
    assert _watermark(knowledge_system) == EXPORT_WATERMARK_MS
    assert PRIVATE_TEXT not in json.dumps(result)


def test_corrupt_xlsx_reports_inspection_stage_and_keeps_watermark(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    xlsx = _authorized_export(knowledge_system, source_root)
    xlsx.write_bytes(b"not-an-xlsx")
    result = run_daily_import(knowledge_system)
    assert result["status"] == "failed"
    assert result["error_code"] == "weflow_inspection_failed"
    assert result["candidate_sessions"] == 1
    assert _watermark(knowledge_system) == EXPORT_WATERMARK_MS


def test_partial_import_reports_reason_counts_without_private_session_details(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorized_export(knowledge_system, source_root)
    monkeypatch.setattr(
        knowledge_system.customer_workflows,
        "import_weflow_exports",
        lambda **_: {
            "status": "warning",
            "result": {
                "imported": 1,
                "failed_sessions": 2,
                "session_errors": [
                    {"error_type": "WeFlowFormatError", "message": PRIVATE_TEXT},
                    {"error_type": "PermissionError", "session_id": PRIVATE_TEXT},
                ],
            },
        },
    )
    result = run_daily_import(knowledge_system)
    assert result["status"] == "warning"
    assert result["failure_counts"] == {"weflow_import_format": 1, "weflow_import_io": 1}
    assert _watermark(knowledge_system) == EXPORT_WATERMARK_MS
    path = worker._write_report(
        knowledge_system,
        {
            "status": "warning",
            "result": {
                "weflow_import": result,
            },
        },
    )
    content = Path(path).read_text(encoding="utf-8")
    assert "weflow_import_format" in content and "weflow_import_io" in content
    assert PRIVATE_TEXT not in content + json.dumps(result)


def test_progress_save_failure_keeps_actual_committed_message_count(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorized_export(knowledge_system, source_root)

    def fail(*_args):
        raise PermissionError(PRIVATE_TEXT)

    monkeypatch.setattr("pkas.weflow_daily._save_state", fail)
    result = run_daily_import(knowledge_system)
    assert result["status"] == "failed"
    assert result["error_code"] == "sync_state_failed"
    assert result["imported_messages"] == 1
    assert _watermark(knowledge_system) == EXPORT_WATERMARK_MS
    assert knowledge_system.repository.stats()["counts"]["customer_messages"] == 1


def test_retention_crash_is_warning_not_lost_import_success(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorized_export(knowledge_system, source_root)

    def fail(**_kwargs):
        raise OSError(PRIVATE_TEXT)

    monkeypatch.setattr("pkas.weflow_daily.prune_verified_exports", fail)
    result = run_daily_import(knowledge_system)
    assert result["status"] == "warning"
    assert result["error_code"] == "weflow_retention_warning"
    assert result["imported_messages"] == 1
    assert result["retention_errors"] == 1
    assert _watermark(knowledge_system) == EXPORT_WATERMARK_MS + 200


def test_outer_state_save_failure_preserves_import_counts(
    knowledge_system: KnowledgeSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args):
        raise PermissionError(PRIVATE_TEXT)

    monkeypatch.setattr("pkas.scheduled_sync._save_state", fail)
    result = run_scheduled_sync(
        knowledge_system,
        now_ms=1000,
        boot_id=0,
        full_sync_runner=lambda _: {"status": "completed"},
        weflow_import_runner=lambda _: {"status": "completed", "imported_messages": 7},
    )
    assert result["status"] == "failed" and result["state_save_failed"]
    path = worker._write_report(knowledge_system, {"status": "failed", "result": result})
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    assert receipt["counts"]["weflow_imported_messages"] == 7
    assert receipt["issues"][0]["code"] == "sync_state_failed"


def test_log_write_failure_returns_safe_nonzero_signal(
    knowledge_system: KnowledgeSystem,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _run_worker(monkeypatch, knowledge_system.settings.data_root, "--local-only")
    monkeypatch.setattr(KnowledgeSystem, "create", lambda: knowledge_system)
    monkeypatch.setattr(
        "pkas.scheduled_sync.run_scheduled_sync",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "weflow_import": {"status": "disabled"},
        },
    )

    def fail(*_args):
        raise PermissionError(PRIVATE_TEXT)

    monkeypatch.setattr(worker, "_write_report_at", fail)
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1
    assert "sync_receipt_failed" in output.err + output.out
    assert PRIVATE_TEXT not in output.err + output.out


def test_cleanup_warning_merges_without_hiding_completed_import(tmp_path: Path) -> None:
    run_id = uuid.uuid4().hex
    path = write_run_receipt(
        tmp_path,
        kind="scheduled-sync",
        status="completed",
        run_id=run_id,
        counts={"weflow_imported_messages": 7},
        issues=[],
    )
    warning = {
        "version": 1,
        "kind": "scheduled-sync",
        "run_id": run_id,
        "recorded_at": "2099-01-01T00:00:00+00:00",
        "status": "warning",
        "counts": {},
        "issues": [issue("weflow_cleanup_failed")],
    }
    (path.parent / "launcher-latest.json").write_text(json.dumps(warning), encoding="utf-8")
    latest = read_run_receipts(tmp_path)["scheduled-sync"]["latest"]
    assert latest["status"] == "warning"
    assert latest["counts"]["weflow_imported_messages"] == 7
    assert latest["issues"][0]["code"] == "weflow_cleanup_failed"
