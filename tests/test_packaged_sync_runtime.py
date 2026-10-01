from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = str(
    Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)


def quoted(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def resolve(project: Path, data: Path, runtime: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "$ErrorActionPreference='Stop'; . "
        + quoted(ROOT / "scripts/resolve_sync_runtime.ps1")
        + "; Resolve-PkasSyncRuntime -ProjectRoot "
        + quoted(project)
        + " -DataRoot "
        + quoted(data)
        + " -RuntimeRoot "
        + quoted(runtime)
        + " | ConvertTo-Json -Compress"
    )
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, check=False,
    )


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path, Path]:
    # Deliberately different install and storage locations, with spaces.
    project = tmp_path / "Installed App/pkas-app"
    data = tmp_path / "Knowledge Data/data"
    runtime = tmp_path / "Knowledge Data/runtime"
    (project / "runtime").mkdir(parents=True)
    (project / "runtime/desktop-runtime.json").write_text("{}", encoding="utf-8")
    data.mkdir(parents=True)
    python = runtime / "python-env/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    return project, data, runtime


def test_installed_sync_resolves_managed_python_and_bundled_node(installed) -> None:
    project, data, runtime = installed
    result = resolve(project, data, runtime)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert context["Packaged"] is True
    assert Path(context["Python"]) == runtime / "python-env/Scripts/python.exe"
    assert Path(context["Node"]) == project / "runtime/node/node.exe"
    assert Path(context["NpmCli"]) == project / "runtime/node/node_modules/npm/bin/npm-cli.js"
    assert Path(context["DataRoot"]) == data


def test_installed_missing_data_does_not_create_an_empty_knowledge_base(installed) -> None:
    project, data, runtime = installed
    missing = data.parent / "missing data"
    result = resolve(project, missing, runtime)
    assert result.returncode != 0
    assert "data drive is unavailable" in result.stderr
    assert not missing.exists()
    assert not (project / "data").exists()


def test_installed_missing_python_does_not_fall_back_to_development(installed) -> None:
    project, data, runtime = installed
    (runtime / "python-env/Scripts/python.exe").unlink()
    fallback = project / ".venv/Scripts/python.exe"
    fallback.parent.mkdir(parents=True)
    fallback.touch()
    result = resolve(project, data, runtime)
    assert result.returncode != 0
    assert "selected Python runtime is unavailable" in result.stderr


def test_development_sync_remains_in_the_selected_checkout(tmp_path: Path) -> None:
    project = tmp_path / "source"
    python = project / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    result = resolve(project, tmp_path / "data", project / "runtime")
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert context["Packaged"] is False
    assert Path(context["Python"]) == python


def test_sync_configuration_requires_explicit_approval_before_any_write(tmp_path: Path) -> None:
    project = tmp_path / "not even an installation"
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "scripts/configure_scheduled_sync.ps1"),
         "-Action", "EnableLocal", "-ProjectRoot", str(project)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "Explicit approval required" in result.stderr
    assert not project.exists()


def test_local_scheduled_worker_runs_from_the_packaged_source(installed) -> None:
    project, data, runtime = installed
    # Isolated data and the existing dependency interpreter; PYTHONPATH must
    # select shipped code rather than an older wheel in that interpreter.
    shutil.copytree(
        ROOT / "src/pkas", project / "src/pkas",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", ". " + quoted(ROOT / "scripts/resolve_sync_runtime.ps1")
         + "; $c=Resolve-PkasSyncRuntime -ProjectRoot " + quoted(project)
         + " -DataRoot " + quoted(data) + " -RuntimeRoot " + quoted(runtime)
         + "; $env:PKAS_PROJECT_ROOT=$c.ProjectRoot; $env:PKAS_DATA_ROOT=$c.DataRoot;"
         + " $env:PKAS_ENV_FILE=Join-Path $c.DataRoot 'config/.env';"
         + " $env:PYTHONPATH=Join-Path $c.ProjectRoot 'src'; & "
         + quoted(ROOT / ".venv/Scripts/python.exe")
         + " -m pkas.scheduled_sync_worker --local-only; exit $LASTEXITCODE"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    receipt = json.loads(
        (data / "runs/diagnostics/scheduled-sync/latest.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "warning"
    assert receipt["counts"]["no_local_roots"] == 1
    assert receipt["counts"]["weflow_enabled"] == 0


@pytest.mark.parametrize("registration_fails", [False, True])
def test_installed_schedule_configuration_and_policy_rollback(
    installed, tmp_path: Path, registration_fails: bool,
) -> None:
    project, data, runtime = installed
    shutil.copytree(ROOT / "scripts", project / "scripts", ignore=lambda _, names: [
        name for name in names if not name.endswith(".ps1") or name.startswith("prune_")
    ])
    config = data / "config"
    config.mkdir()
    original = b'{ "schema_version": 1, "autostart_enabled": false }\r\n'
    policy = config / "windows_autostart_policy.json"
    policy.write_bytes(original)
    # Scheduler commands are mocked in the same PS5 process. No real task,
    # profile, installed application, or personal database is changed.
    wrapper = tmp_path / "mock-scheduler.ps1"
    mock = r'''
$ErrorActionPreference='Stop'
$global:MockTasks=@{}
function Get-ScheduledTask { [CmdletBinding()]param($TaskName) $global:MockTasks[$TaskName] }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory)
    [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }
function New-ScheduledTaskTrigger { param([switch]$Daily,$At,[switch]$AtLogOn,$User)
    [pscustomobject]@{Delay='';At=$At;Daily=[bool]$Daily} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel)
    [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel} }
function New-ScheduledTaskSettingsSet { param([switch]$AllowStartIfOnBatteries,
    [switch]$DontStopIfGoingOnBatteries,[switch]$StartWhenAvailable,[switch]$Hidden,
    $MultipleInstances,$ExecutionTimeLimit)
    [pscustomobject]@{MultipleInstances=$MultipleInstances} }
function New-ScheduledTask { param($Action,$Trigger,$Principal,$Settings,$Description)
    [pscustomobject]@{Actions=@($Action);Triggers=@($Trigger);Principal=$Principal;
        Settings=$Settings;Description=$Description;TaskName='';State='Ready'} }
function Register-ScheduledTask { param($TaskName,$InputObject,$Xml,[switch]$Force)
    if ($global:RegistrationFails) { throw 'Injected scheduler registration failure' }
    $InputObject.TaskName=$TaskName; $global:MockTasks[$TaskName]=$InputObject }
function Unregister-ScheduledTask { param($TaskName,$Confirm) $global:MockTasks.Remove($TaskName) }
function Export-ScheduledTask { param($TaskName) '<Task />' }
'''
    mock += "$global:RegistrationFails=" + ("$true" if registration_fails else "$false")
    mock += "; try { & " + quoted(project / "scripts/configure_scheduled_sync.ps1")
    mock += " -Action EnableLocal -Approve -ProjectRoot " + quoted(project)
    mock += " -DataRoot " + quoted(data) + " -RuntimeRoot " + quoted(runtime)
    mock += "; $global:MockTasks['PKAS-Knowledge-Sync'] | ConvertTo-Json -Depth 6 -Compress"
    mock += "; } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 3 }"
    wrapper.write_text(mock, encoding="utf-8")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper)], capture_output=True, text=True, check=False,
    )
    checkpoint = json.loads((config / "sync-task-recovery.json").read_text(encoding="utf-8"))
    assert checkpoint["scope"] == "scheduled-sync-settings"
    assert checkpoint["policy_existed"] is True
    assert not (data / "index/pkas.sqlite").exists()
    if registration_fails:
        assert result.returncode == 3, result.stdout + result.stderr
        assert "Injected scheduler registration failure" in result.stderr
        assert policy.read_bytes() == original
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        task = json.loads(result.stdout.splitlines()[-1])
        action = task["Actions"][0]
        assert str(runtime) in action["Arguments"]
        assert str(data) in action["Arguments"]
        assert "-LocalOnly" in action["Arguments"]
        assert task["Principal"]["RunLevel"] == "Limited"
        assert len(task["Triggers"]) == 2
        assert json.loads(policy.read_text())["autostart_enabled"] is True
