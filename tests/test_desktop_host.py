from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_launcher_is_manual_hidden_and_does_not_register_autostart() -> None:
    launcher = (ROOT / "PKAS-Launcher.vbs").read_text(encoding="utf-8")
    stopper = (ROOT / "PKAS-Stop.vbs").read_text(encoding="utf-8")
    host = (ROOT / "scripts" / "pkas_desktop_host.ps1").read_text(encoding="utf-8")

    assert "WindowStyle Hidden" in launcher
    assert "launch_desktop_hidden.ps1" in launcher
    assert "WindowStyle Hidden" in stopper
    assert "stop_desktop_host.ps1" in stopper
    assert "Register-ScheduledTask" not in launcher + host
    assert "windows_autostart_policy" not in launcher + host
    assert "WeFlow" not in launcher + host
    assert (ROOT / "scripts" / "pkas_desktop_host.ps1").read_bytes().startswith(
        b"\xef\xbb\xbf"
    )


def test_desktop_host_owns_expected_services_and_exact_shutdown_gate() -> None:
    host = (ROOT / "scripts" / "pkas_desktop_host.ps1").read_text(encoding="utf-8")

    assert "pythonw.exe" in host
    assert "pkas.cli" in host
    assert "pkas.core_worker" in host
    assert "start_qdrant.ps1" in host
    assert "Local\\PKASDesktopHost" in host
    assert "Stop-ExactManagedProcess" in host
    assert 'candidate.CommandLine -notlike "*$resolvedRoot*"' in host
    assert "--app=$dashboardUrl" in host
    assert "System.Windows.Forms.NotifyIcon" in host


def test_desktop_stop_script_refuses_processes_outside_project_boundary() -> None:
    stopper = (ROOT / "scripts" / "stop_desktop_host.ps1").read_text(encoding="utf-8")

    assert "Stop-VerifiedProcess" in stopper
    assert 'candidate.CommandLine -notlike "*$resolvedRoot*"' in stopper
    assert "launch_desktop_hidden.ps1" in stopper
    assert "Refused to stop PID" in stopper
    assert "Get-Process | Stop-Process" not in stopper
