from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAURI_ROOT = ROOT / "desktop" / "src-tauri"


def test_tauri_config_builds_a_current_user_windows_installer() -> None:
    config = json.loads((TAURI_ROOT / "tauri.conf.json").read_text(encoding="utf-8"))

    assert config["productName"] == "知域"
    assert config["version"] == "0.1.2"
    assert config["identifier"] == "com.zhishu.pkas"
    assert config["build"]["frontendDist"] == "../../web/dist"
    assert config["bundle"]["active"] is True
    assert config["bundle"]["targets"] == ["nsis"]
    assert config["bundle"]["windows"]["nsis"]["installMode"] == "currentUser"
    assert config["bundle"]["windows"]["webviewInstallMode"]["type"] == "downloadBootstrapper"
    resources = config["bundle"]["resources"]
    assert "../../src/pkas/" in resources
    assert "../../pyproject.toml" in resources
    assert "../../uv.lock" in resources
    assert "../../web/dist/" in resources
    assert "../../runtime/tauri-payload/" in resources
    assert "../../tools/everything/" in resources
    assert "../../scripts/configure_weflow_manual.mjs" in resources
    assert config["app"]["windows"] == []


def test_desktop_product_copy_uses_clear_chinese_titles_without_internal_badges() -> None:
    app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert 'title: "个人知识工作台"' in app
    assert 'title: "资料底座"' in app
    assert 'title: "运行状态"' in app
    assert 'title: "知识检索"' in app
    assert "本地运行 · 数据由你控制" in app
    assert "ACTIVE AXIS" not in app
    assert "LOCAL · PRIVATE" not in app
    assert "<span>{code}</span>" not in app
    assert ".panel-head span { display: none; }" in styles


def test_tauri_shell_has_single_instance_tray_and_close_to_hide() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")

    assert source.index("tauri_plugin_single_instance::init") < source.index(".setup(")
    assert 'format!("http://{PKAS_HOST}:{PKAS_PORT}/")' in source
    assert "TrayIconBuilder" in source
    assert "api.prevent_close()" in source
    assert 'argument == "--quit"' in source


def test_tauri_startup_does_not_start_sensitive_or_scheduled_integrations() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8").lower()
    startup = source[source.index("fn start_application") : source.index("fn show_main_window")]

    forbidden = (
        "weflow",
        "mcp_server",
        "knowledge-sync",
        "daily-backup",
        "schtasks",
        "register-scheduledtask",
    )
    assert all(item not in startup for item in forbidden)


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
    assert source.index('bind_backend_lifetime()?;') < source.index('std::thread::spawn')
    assert 'let mut backend = match start_backend(&paths)' in source
    assert 'Err(error) => {' in source
    assert 'if let Ok(Some(_)) = backend.try_wait()' in source
    assert '本地知识服务启动后提前退出。' in source
    assert '退出并停止本机服务' in source
    assert '检测到已有后台服务' in source
    assert source.index('start_backend(&paths)') < source.index(
        'start_qdrant_via_backend(&paths, window)'
    )
    assert '"/api/runtime/services/qdrant"' in source
    assert 'Command::new(&paths.qdrant)' not in source


def test_tauri_shell_has_portable_root_resolution_without_secrets() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
    assert "PKAS_PROJECT_ROOT" in source
    assert 'join("Zhishu").join("pkas-root.txt")' in source
    assert "valid_project_root" in source
    assert "remember_project_root" in source
    assert "未找到有效的知域程序资源" in source
    assert "api_key" not in source.lower()


def test_packaged_first_run_is_isolated_and_does_not_autoscan_or_configure_clients() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")
    startup = (ROOT / "web" / "public" / "startup.html").read_text(encoding="utf-8")

    assert 'path.join("Zhishu")' in source
    assert 'home.join("data")' in source
    assert 'runtime.join("python-env/Scripts/pythonw.exe")' in source
    assert 'runtime/qdrant/qdrant.exe' in source
    assert 'runtime/node/node_modules/npm/bin/npm-cli.js' in source
    assert '"UV_PROJECT_ENVIRONMENT"' in source
    assert '"PKAS_DATA_ROOT"' in source
    assert '"PKAS_QDRANT_URL"' in source
    assert "UV_PYTHON_INSTALL_DIR" in source
    assert "UV_CACHE_DIR" in source
    assert "creation_flags(0x08000000)" in source
    assert "不会自动扫描磁盘" in source
    assert "退出并停止本机服务" in source
    assert "setStartupState" in startup
    assert "setStartupError" in startup
    assert "开始第一次资料接入" in (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")


def test_runtime_page_uses_a_fast_service_state_before_loading_history() -> None:
    source = (ROOT / "web" / "src" / "RuntimeCenter.tsx").read_text(encoding="utf-8")

    assert '"/api/runtime/brief"' in source
    assert '"/api/runtime/overview"' in source
    assert "RUNTIME_CACHE_KEY" in source
    assert "25000" in source


def test_packaged_storage_keeps_large_data_off_system_drive_and_preserves_existing_data() -> None:
    source = (TAURI_ROOT / "src" / "main.rs").read_text(encoding="utf-8")

    assert "GetLogicalDriveStringsW" in source
    assert "GetDiskFreeSpaceExW" in source
    assert "MIN_NON_SYSTEM_DATA_FREE_BYTES" in source
    assert 'join("storage-root.txt")' in source
    assert "legacy_data_has_user_content" in source
    assert "legacy_project_data_root" in source
    assert "choose_storage_locations" in source
    assert '"PKAS_RUNTIME_ROOT"' in source
    assert "NotConnected" in source
    assert '"storage"' in (ROOT / "src" / "pkas" / "runtime_manager.py").read_text(
        encoding="utf-8"
    )
    assert '"/api/runtime/storage"' in (ROOT / "src" / "pkas" / "api.py").read_text(
        encoding="utf-8"
    )


def test_tauri_resource_manifest_pins_only_external_runtime_binaries() -> None:
    manifest = json.loads((ROOT / "config" / "desktop_runtime.json").read_text(encoding="utf-8"))
    prepare_script = (ROOT / "scripts" / "prepare_tauri_resources.ps1").read_text(encoding="utf-8")

    assert manifest["python_version"] == "3.11"
    assert manifest["uv"]["version"]
    assert len(manifest["uv"]["sha256"]) == 64
    assert manifest["qdrant"]["version"] == "1.19.0"
    assert len(manifest["qdrant"]["sha256"]) == 64
    assert manifest["node"]["version"] == "24.20.0"
    assert len(manifest["node"]["sha256"]) == 64
    assert manifest["everything"]["version"] == "1.4.1.1032"
    assert len(manifest["everything"]["executable_sha256"]) == 64
    assert "Get-FileHash" in prepare_script
    assert "SHA-256 verification" in prepare_script
    assert "runtime\\tauri-payload" in prepare_script
    assert "Copy-Item -LiteralPath $qdrantSource" in prepare_script
    assert "node_modules\\npm\\bin\\npm-cli.js" in prepare_script
