import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_windows_replication_manifest_is_pinned() -> None:
    manifest = json.loads(
        (ROOT / "config" / "windows_replication.json").read_text(encoding="utf-8")
    )

    assert manifest["schema_version"] == 1
    assert manifest["qdrant"]["version"] == "1.19.0"
    assert manifest["qdrant"]["url"].startswith(
        "https://github.com/qdrant/qdrant/releases/download/v1.19.0/"
    )
    assert len(manifest["qdrant"]["sha256"]) == 64


def test_replication_scripts_cover_full_lifecycle() -> None:
    expected = {
        "preflight_windows.ps1",
        "export_windows_package.ps1",
        "bootstrap_windows.ps1",
        "verify_windows_replica.ps1",
        "install_qdrant_windows.ps1",
        "install_qdrant_task.ps1",
        "install_backup_task.ps1",
        "run_backup.ps1",
        "uninstall_backup_task.ps1",
        "stop_pkas_runtime.ps1",
        "uninstall_windows_runtime.ps1",
        "assert_windows_autostart_allowed.ps1",
    }
    assert expected <= {path.name for path in (ROOT / "scripts").glob("*.ps1")}

    bootstrap = (ROOT / "scripts" / "bootstrap_windows.ps1").read_text(encoding="utf-8")
    assert "--no-editable" in bootstrap
    assert "mcp_configuration_changed = $false" in bootstrap
    assert '"$TaskPrefix-Daily-Backup"' in bootstrap


def test_all_task_installers_honor_local_autostart_policy() -> None:
    installers = (
        "install_qdrant_task.ps1",
        "install_core_worker.ps1",
        "install_dashboard.ps1",
        "install_backup_task.ps1",
        "install_scheduled_sync.ps1",
    )
    guard_name = "assert_windows_autostart_allowed.ps1"

    for installer in installers:
        content = (ROOT / "scripts" / installer).read_text(encoding="utf-8")
        assert guard_name in content

    bootstrap = (ROOT / "scripts" / "bootstrap_windows.ps1").read_text(
        encoding="utf-8"
    )
    assert guard_name in bootstrap


def test_scheduled_sync_is_a_hidden_daily_trigger() -> None:
    installer = (ROOT / "scripts" / "install_scheduled_sync.ps1").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "scripts" / "run_scheduled_sync.ps1").read_text(
        encoding="utf-8"
    )

    assert "[string]$DailyAt = '00:00'" in installer
    assert "New-ScheduledTaskTrigger -Daily -At $dailyTime" in installer
    assert "loginTrigger" not in installer
    assert "watchdogTrigger" not in installer
    assert "-check-daily-authorization" in runner
    assert "$dailyImportEnabled -and $weflowProcesses.Count -eq 0" in runner
    assert "$env:WEFLOW_ROOT = $WeFlowRoot" in runner
    assert "D:\\wx\\xwechat_files\\tools\\WeFlow" not in runner
    bootstrap = (ROOT / "scripts" / "bootstrap_windows.ps1").read_text(encoding="utf-8")
    assert "[string]$DailyAt = '00:00'" in bootstrap
    assert "-DailyAt $DailyAt" in bootstrap


def test_autostart_policy_gate_fails_closed(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None
    guard = ROOT / "scripts" / "assert_windows_autostart_allowed.ps1"
    data_root = tmp_path / "data"

    missing = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(guard),
            "-ProjectRoot",
            str(ROOT),
            "-DataRoot",
            str(data_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert "denied by default" in (missing.stdout + missing.stderr)

    config_root = data_root / "config"
    config_root.mkdir(parents=True)
    policy_path = config_root / "windows_autostart_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "autostart_enabled": False,
                "task_registration_requires_user_reapproval": True,
            }
        ),
        encoding="utf-8",
    )
    disabled = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(guard),
            "-ProjectRoot",
            str(ROOT),
            "-DataRoot",
            str(data_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert disabled.returncode != 0
    assert "is disabled by local user policy" in (disabled.stdout + disabled.stderr)


def test_autostart_policy_gate_accepts_explicit_enabled_policy(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None
    guard = ROOT / "scripts" / "assert_windows_autostart_allowed.ps1"
    data_root = tmp_path / "data"
    config_root = data_root / "config"
    config_root.mkdir(parents=True)
    (config_root / "windows_autostart_policy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "autostart_enabled": True,
                "task_registration_requires_user_reapproval": False,
            }
        ),
        encoding="utf-8",
    )

    enabled = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(guard),
            "-ProjectRoot",
            str(ROOT),
            "-DataRoot",
            str(data_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert enabled.returncode == 0, enabled.stdout + enabled.stderr


def test_qdrant_template_has_no_machine_specific_storage_path() -> None:
    template = (ROOT / "config" / "qdrant.yaml").read_text(encoding="utf-8")
    assert "E:/codex-kb" not in template
    assert "storage_path:" not in template
    assert "snapshots_path:" not in template


def test_exporter_excludes_personal_and_secret_artifacts() -> None:
    exporter = (ROOT / "scripts" / "export_windows_package.ps1").read_text(
        encoding="utf-8"
    )
    for marker in ("'data'", "'.venv'", "'runtime'", "'.env'", "'*.dpapi'", "'*.sqlite'"):
        assert marker in exporter
