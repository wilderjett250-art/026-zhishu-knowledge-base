from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAURI_ROOT = ROOT / "desktop" / "src-tauri"


def test_tauri_config_is_local_and_does_not_bundle_yet() -> None:
    config = json.loads((TAURI_ROOT / "tauri.conf.json").read_text(encoding="utf-8"))

    assert config["identifier"] == "com.zhishu.pkas"
    assert config["build"]["frontendDist"] == "../../web/dist"
    assert config["bundle"]["active"] is False
    assert config["app"]["windows"] == []


def test_tauri_shell_has_single_instance_tray_and_close_to_hide() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")

    assert source.index("tauri_plugin_single_instance::init") < source.index(".setup(")
    assert "http://127.0.0.1:8765/" in source
    assert "TrayIconBuilder" in source
    assert "api.prevent_close()" in source
    assert 'argument == "--quit"' in source


def test_tauri_shell_does_not_start_sensitive_or_scheduled_integrations() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8").lower()

    forbidden = (
        "weflow",
        "mcp_server",
        "knowledge-sync",
        "daily-backup",
        "schtasks",
        "register-scheduledtask",
    )
    assert all(item not in source for item in forbidden)


def test_tauri_frontend_has_no_shell_or_process_permissions() -> None:
    capability = json.loads(
        (TAURI_ROOT / "capabilities" / "default.json").read_text(encoding="utf-8")
    )

    assert capability["windows"] == ["main"]
    assert set(capability["permissions"]) == {
        "core:default", "core:window:allow-minimize",
        "core:window:allow-toggle-maximize", "core:window:allow-close",
        "core:window:allow-start-dragging",
    }


def test_backend_lifetime_is_bound_after_tray_creation() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
    assert '.icon(tray_icon())' in source
    assert 'JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE' in source
    assert 'AssignProcessToJobObject(job, GetCurrentProcess())' in source
    assert source.index('.build(app)?;') < source.index('bind_backend_lifetime()?;')
    assert source.index('bind_backend_lifetime()?;') < source.index('if start_backend().is_err()')
    assert '退出知枢并停止后台' in source
    assert '检测到旧版独立后台' in source


def test_tauri_shell_has_portable_root_resolution_without_secrets() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
    assert "PKAS_PROJECT_ROOT" in source
    assert 'join("Zhishu").join("pkas-root.txt")' in source
    assert "valid_project_root" in source
    assert "remember_project_root" in source
    assert "未找到知识库目录" in source
    assert "api_key" not in source.lower()
