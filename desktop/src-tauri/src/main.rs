#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};

#[cfg(windows)]
use std::os::windows::ffi::OsStringExt;

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent,
};

const MAIN_WINDOW_LABEL: &str = "main";
const PKAS_HOST: &str = "127.0.0.1";
const PKAS_PORT: u16 = 8765;
const QDRANT_HTTP_PORT: u16 = 6333;
const QDRANT_GRPC_PORT: u16 = 6334;
const PACKAGED_PROJECT_FOLDER: &str = "pkas-app";

#[derive(Clone)]
struct RuntimePaths {
    project_root: PathBuf,
    data_root: PathBuf,
    app_home: PathBuf,
    pythonw: PathBuf,
    uv: PathBuf,
    runtime_manifest: PathBuf,
    packaged: bool,
}

#[derive(Clone, Debug)]
struct PackagedStorageLocations {
    app_home: PathBuf,
    data_root: PathBuf,
}

fn valid_project_root(root: &Path) -> bool {
    root.join("src/pkas/api.py").is_file() && root.join(".venv/Scripts/pythonw.exe").is_file()
}

fn valid_packaged_root(root: &Path) -> bool {
    root.join("src/pkas/api.py").is_file()
        && root.join("pyproject.toml").is_file()
        && root.join("uv.lock").is_file()
        && root.join("web/dist/index.html").is_file()
        && root.join("runtime/uv.exe").is_file()
        && root.join("runtime/qdrant/qdrant.exe").is_file()
        && root.join("runtime/node/node.exe").is_file()
        && root
            .join("runtime/node/node_modules/npm/bin/npm-cli.js")
            .is_file()
        && root.join("tools/everything/everything.exe").is_file()
        && root.join("scripts/configure_weflow_manual.mjs").is_file()
        && root.join("runtime/desktop-runtime.json").is_file()
}

fn root_config_path() -> Option<PathBuf> {
    std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .map(|appdata| appdata.join("Zhishu").join("pkas-root.txt"))
}

fn configured_project_root() -> Option<PathBuf> {
    let raw = std::env::var_os("PKAS_PROJECT_ROOT")
        .map(PathBuf::from)
        .or_else(|| {
            root_config_path()
                .and_then(|path| fs::read_to_string(path).ok())
                .map(|value| PathBuf::from(value.trim()))
        })?;
    raw.canonicalize()
        .ok()
        .filter(|path| valid_project_root(path))
}

fn executable_project_root() -> std::io::Result<Option<PathBuf>> {
    let executable = std::env::current_exe()?;
    Ok(executable
        .ancestors()
        .find(|path| valid_project_root(path))
        .map(Path::to_path_buf))
}

fn executable_packaged_root() -> std::io::Result<Option<PathBuf>> {
    let executable = std::env::current_exe()?;
    Ok(executable
        .parent()
        .map(|directory| directory.join(PACKAGED_PROJECT_FOLDER))
        .filter(|path| valid_packaged_root(path)))
}

fn packaged_project_root<R: Runtime>(app: &AppHandle<R>) -> Option<PathBuf> {
    app.path()
        .resource_dir()
        .ok()
        .map(|resource_dir| resource_dir.join(PACKAGED_PROJECT_FOLDER))
        .filter(|path| valid_packaged_root(path))
}

fn project_root<R: Runtime>(app: &AppHandle<R>) -> std::io::Result<PathBuf> {
    // Packaged resources take priority over a stale portable-root setting. A
    // newly installed app must not silently attach itself to another project.
    if let Some(root) = executable_project_root()? {
        return Ok(root);
    }
    // An installed EXE must prefer the resources shipped next to itself. Tauri's
    // generic resource lookup can otherwise resolve a host application's cache.
    if let Some(root) = executable_packaged_root()? {
        return Ok(root);
    }
    if let Some(root) = packaged_project_root(app) {
        return Ok(root);
    }
    if let Some(root) = configured_project_root() {
        return Ok(root);
    }
    Err(std::io::Error::new(
        std::io::ErrorKind::NotFound,
        "未找到有效的知域程序资源。请重新运行正式安装包修复安装。",
    ))
}

fn remember_project_root(root: &Path) -> std::io::Result<()> {
    let Some(path) = root_config_path() else {
        return Ok(());
    };
    fs::create_dir_all(path.parent().expect("config path has parent"))?;
    if path.exists() {
        let desired = root.to_string_lossy();
        if fs::read_to_string(&path).is_ok_and(|current| current.trim() == desired.as_ref()) {
            return Ok(());
        }
        return Err(std::io::Error::new(
            std::io::ErrorKind::AlreadyExists,
            "已有项目根配置与当前项目不同；为保护原配置，本次未覆盖。",
        ));
    }
    let temporary = path.with_extension("tmp");
    fs::write(&temporary, root.to_string_lossy().as_bytes())?;
    fs::rename(temporary, path)
}

fn local_app_home() -> std::io::Result<PathBuf> {
    std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .map(|path| path.join("Zhishu"))
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "无法定位当前 Windows 用户的本地应用数据目录。",
            )
        })
}

#[derive(Clone, Debug)]
struct DataVolume {
    root: PathBuf,
    free_bytes: u64,
    fixed: bool,
}

const MIN_NON_SYSTEM_DATA_FREE_BYTES: u64 = 10 * 1024 * 1024 * 1024;

fn choose_storage_home(local_home: &Path, system_drive: &str, volumes: &[DataVolume]) -> PathBuf {
    volumes
        .iter()
        .filter(|volume| {
            let drive = volume.root.to_string_lossy();
            let drive = drive.trim_end_matches('\\');
            volume.fixed
                && volume.free_bytes >= MIN_NON_SYSTEM_DATA_FREE_BYTES
                && !drive.eq_ignore_ascii_case(system_drive.trim_end_matches('\\'))
        })
        .max_by_key(|volume| volume.free_bytes)
        .map(|volume| volume.root.join("Zhishu"))
        .unwrap_or_else(|| local_home.to_path_buf())
}

fn choose_storage_locations(
    local_home: &Path,
    system_drive: &str,
    volumes: &[DataVolume],
    has_legacy_local_data: bool,
    legacy_project_data: Option<PathBuf>,
) -> std::io::Result<PackagedStorageLocations> {
    if has_legacy_local_data && legacy_project_data.is_some() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "检测到两处已有知识库；为避免选错或覆盖数据，知域未自动选择。",
        ));
    }

    let app_home = if has_legacy_local_data {
        local_home.to_path_buf()
    } else {
        choose_storage_home(local_home, system_drive, volumes)
    };
    let data_root = if has_legacy_local_data {
        local_home.join("data")
    } else if let Some(existing_data) = legacy_project_data {
        existing_data
    } else {
        app_home.join("data")
    };
    Ok(PackagedStorageLocations {
        app_home,
        data_root,
    })
}

#[cfg(windows)]
fn data_volumes() -> Vec<DataVolume> {
    use windows_sys::Win32::Storage::FileSystem::{
        GetDiskFreeSpaceExW, GetDriveTypeW, GetLogicalDriveStringsW,
    };

    let mut buffer = vec![0u16; 512];
    let length = unsafe { GetLogicalDriveStringsW(buffer.len() as u32, buffer.as_mut_ptr()) };
    if length == 0 || length as usize >= buffer.len() {
        return Vec::new();
    }

    buffer[..length as usize]
        .split(|unit| *unit == 0)
        .filter(|drive| !drive.is_empty())
        .filter_map(|drive| {
            let mut wide = drive.to_vec();
            wide.push(0);
            let fixed = unsafe { GetDriveTypeW(wide.as_ptr()) } == 3;
            if !fixed {
                return None;
            }
            let mut available = 0u64;
            let result = unsafe {
                GetDiskFreeSpaceExW(
                    wide.as_ptr(),
                    &mut available,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            };
            if result == 0 {
                return None;
            }
            let root = PathBuf::from(std::ffi::OsString::from_wide(drive));
            Some(DataVolume {
                root,
                free_bytes: available,
                fixed,
            })
        })
        .collect()
}

#[cfg(not(windows))]
fn data_volumes() -> Vec<DataVolume> {
    Vec::new()
}

fn legacy_data_has_user_content(data_root: &Path) -> std::io::Result<bool> {
    if !data_root.exists() {
        return Ok(false);
    }
    for entry in fs::read_dir(data_root)? {
        let entry = entry?;
        if entry.file_name().to_string_lossy() != "runtime" {
            return Ok(true);
        }
    }
    Ok(false)
}

fn legacy_project_data_root() -> Option<PathBuf> {
    let configured_root = fs::read_to_string(root_config_path()?).ok()?;
    let root = PathBuf::from(configured_root.trim()).canonicalize().ok()?;
    let data_root = root.join("data");
    let database = data_root.join("index/pkas.sqlite");
    let metadata = fs::metadata(database).ok()?;
    (metadata.is_file() && metadata.len() > 0).then_some(data_root)
}

fn packaged_storage_locations() -> std::io::Result<PackagedStorageLocations> {
    let local_home = local_app_home()?;
    let pointer = local_home.join("storage-root.txt");
    if pointer.exists() {
        let contents = fs::read_to_string(&pointer)?;
        let mut paths = contents
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty());
        let app_home = PathBuf::from(paths.next().unwrap_or_default());
        let data_root = paths
            .next()
            .map(PathBuf::from)
            .unwrap_or_else(|| app_home.join("data"));
        if !app_home.is_absolute() || !data_root.is_absolute() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "知识库数据位置配置无效；为避免创建空白库，启动已停止。",
            ));
        }
        if !data_root.is_dir() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotConnected,
                "知识库数据盘暂不可用；请连接原数据盘后重试，知域不会切换到空目录。",
            ));
        }
        fs::create_dir_all(&app_home).map_err(|error| {
            std::io::Error::new(
                std::io::ErrorKind::NotConnected,
                format!("知识库运行环境位置暂不可用：{error}"),
            )
        })?;
        return Ok(PackagedStorageLocations {
            app_home,
            data_root,
        });
    }

    // Older installs kept data under LocalAppData. Preserve any existing
    // database/config; older portable installs are also linked through
    // pkas-root.txt and must keep using their existing project data directory.
    let legacy_data = local_home.join("data");
    let locations = choose_storage_locations(
        &local_home,
        &std::env::var("SystemDrive").unwrap_or_else(|_| "C:".to_string()),
        &data_volumes(),
        legacy_data_has_user_content(&legacy_data)?,
        legacy_project_data_root(),
    )?;
    fs::create_dir_all(&locations.app_home)?;
    fs::create_dir_all(&locations.data_root)?;
    fs::create_dir_all(&local_home)?;
    let temporary = pointer.with_extension("tmp");
    let contents = format!(
        "{}\n{}\n",
        locations.app_home.display(),
        locations.data_root.display()
    );
    fs::write(&temporary, contents.as_bytes())?;
    fs::rename(temporary, pointer)?;
    Ok(locations)
}

fn runtime_paths<R: Runtime>(app: &AppHandle<R>) -> std::io::Result<RuntimePaths> {
    let root = project_root(app)?;
    // `project_root` can intentionally prefer the packaged resources adjacent
    // to the installed EXE. Do not re-query Tauri's generic resource lookup
    // here: in a host/cache environment it can resolve a different valid
    // package and make this installed root look like a development checkout.
    let packaged = valid_packaged_root(&root);
    if packaged {
        let locations = packaged_storage_locations()?;
        let runtime = locations.app_home.join("runtime");
        Ok(RuntimePaths {
            project_root: root.clone(),
            data_root: locations.data_root,
            app_home: locations.app_home,
            pythonw: runtime.join("python-env/Scripts/pythonw.exe"),
            uv: root.join("runtime/uv.exe"),
            runtime_manifest: root.join("runtime/desktop-runtime.json"),
            packaged: true,
        })
    } else {
        let pythonw = root.join(".venv/Scripts/pythonw.exe");
        if !pythonw.is_file() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "当前程序目录没有 Python 运行环境。",
            ));
        }
        Ok(RuntimePaths {
            project_root: root.clone(),
            data_root: root.join("data"),
            app_home: root.clone(),
            pythonw,
            uv: root.join("runtime/tauri-payload/uv.exe"),
            runtime_manifest: root.join("runtime/tauri-payload/desktop-runtime.json"),
            packaged: false,
        })
    }
}

fn append_log(path: &Path, message: &str) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut log) = OpenOptions::new().create(true).append(true).open(path) {
        let _ = writeln!(log, "{}", message);
    }
}

fn set_startup_status<R: Runtime>(
    window: &WebviewWindow<R>,
    stage: &str,
    title: &str,
    detail: &str,
) {
    let state = serde_json::json!({ "stage": stage, "title": title, "detail": detail });
    let _ = window.eval(&format!("window.setStartupState({})", state));
}

fn set_startup_error<R: Runtime>(window: &WebviewWindow<R>, title: &str, detail: &str) {
    let state = serde_json::json!({ "title": title, "detail": detail });
    let _ = window.eval(&format!("window.setStartupError({})", state));
}

fn tray_icon() -> tauri::image::Image<'static> {
    let mut pixels = vec![0u8; 32 * 32 * 4];
    for y in 0i32..32 {
        for x in 0i32..32 {
            let offset = ((y * 32 + x) * 4) as usize;
            let color = if (x - 16).abs() + (y - 16).abs() < 12 {
                [58, 211, 209, 255]
            } else {
                [20, 32, 44, 255]
            };
            pixels[offset..offset + 4].copy_from_slice(&color);
        }
    }
    tauri::image::Image::new_owned(pixels, 32, 32)
}

fn http_request(port: u16, method: &str, path: &str, body: &str) -> Option<String> {
    let address = format!("{PKAS_HOST}:{port}").parse().ok()?;
    let mut stream =
        std::net::TcpStream::connect_timeout(&address, Duration::from_millis(250)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {PKAS_HOST}:{port}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(request.as_bytes());
    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    Some(response)
}

fn http_response(port: u16, path: &str) -> Option<String> {
    http_request(port, "GET", path, "")
}

fn backend_ready() -> bool {
    http_response(PKAS_PORT, "/api/runtime/ping")
        .is_some_and(|response| response.contains("pkas-runtime"))
}

fn backend_port_occupied() -> bool {
    std::net::TcpStream::connect_timeout(
        &format!("{PKAS_HOST}:{PKAS_PORT}")
            .parse()
            .expect("valid local socket"),
        Duration::from_millis(150),
    )
    .is_ok()
}

fn qdrant_ready() -> bool {
    http_response(QDRANT_HTTP_PORT, "/")
        .is_some_and(|response| response.contains("qdrant - vector search engine"))
}

fn port_occupied(port: u16) -> bool {
    std::net::TcpStream::connect_timeout(
        &format!("{PKAS_HOST}:{port}")
            .parse()
            .expect("valid local socket"),
        Duration::from_millis(150),
    )
    .is_ok()
}

#[cfg(target_os = "windows")]
fn hide_console(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x08000000);
}

#[cfg(not(target_os = "windows"))]
fn hide_console(_command: &mut Command) {}

// Keep the desktop and every child process started by it in one OS-owned
// lifetime. Windows terminates the children when the user exits from the tray.
#[cfg(target_os = "windows")]
fn bind_backend_lifetime() -> std::io::Result<()> {
    use windows_sys::Win32::{
        Foundation::CloseHandle,
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::GetCurrentProcess,
        },
    };
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(std::io::Error::last_os_error());
        }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const _,
            std::mem::size_of_val(&limits) as u32,
        ) == 0
            || AssignProcessToJobObject(job, GetCurrentProcess()) == 0
        {
            let error = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(error);
        }
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn bind_backend_lifetime() -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "backend ownership is Windows-only",
    ))
}

fn wait_child(child: &mut Child, timeout: Duration) -> std::io::Result<()> {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait()? {
            if status.success() {
                return Ok(());
            }
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "本地运行组件安装失败，查看 setup.log 获取诊断信息。",
            ));
        }
        if std::time::Instant::now() >= deadline {
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "本地运行组件安装超时。",
            ));
        }
        std::thread::sleep(Duration::from_millis(300));
    }
}

fn run_hidden_and_log(
    mut command: Command,
    log_path: &Path,
    timeout: Duration,
) -> std::io::Result<()> {
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)?;
    command
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    hide_console(&mut command);
    let mut child = command.spawn()?;
    wait_child(&mut child, timeout)
}

fn python_environment_fingerprint(manifest: &str) -> Option<String> {
    let parsed: serde_json::Value = serde_json::from_str(manifest).ok()?;
    let dependency_lock_sha256 = parsed
        .get("dependency_lock_sha256")
        // Older installations used the entire uv.lock hash. Fall back once so
        // they safely rebuild, then persist the dependency-only fingerprint.
        .or_else(|| parsed.get("uv_lock_sha256"))?;
    // The packaged source is imported directly from the installed application
    // directory. Rebuild the Python environment only when its runtime or
    // dependency lock changes, not for an ordinary source-code update.
    let fingerprint = serde_json::json!({
        "python_version": parsed.get("python_version")?,
        "uv_version": parsed.get("uv_version")?,
        "uv_sha256": parsed.get("uv_sha256")?,
        "dependency_lock_sha256": dependency_lock_sha256
    });
    Some(fingerprint.to_string())
}

fn source_import_root(project_root: &Path) -> PathBuf {
    project_root.join("src")
}

fn ensure_python_environment<R: Runtime>(
    paths: &RuntimePaths,
    window: &WebviewWindow<R>,
) -> std::io::Result<()> {
    if !paths.packaged {
        return paths.pythonw.is_file().then_some(()).ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "本机 Python 环境不存在；请从项目运行 bootstrap_windows.ps1。",
            )
        });
    }

    let manifest = fs::read_to_string(&paths.runtime_manifest)?;
    let manifest_fingerprint = python_environment_fingerprint(&manifest).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "安装包的 Python 运行清单不完整，请修复安装。",
        )
    })?;
    let runtime_dir = paths.app_home.join("runtime");
    let applied_manifest = runtime_dir.join("desktop-runtime.json");
    let setup_log = runtime_dir.join("setup.log");
    let current_manifest = fs::read_to_string(&applied_manifest).unwrap_or_default();
    if paths.pythonw.is_file()
        && python_environment_fingerprint(&current_manifest).as_deref()
            == Some(manifest_fingerprint.as_str())
    {
        return Ok(());
    }

    if !paths.uv.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "安装包缺少 Python 环境准备器。",
        ));
    }
    fs::create_dir_all(&runtime_dir)?;
    fs::create_dir_all(paths.app_home.join("python"))?;
    let uv_cache = paths.app_home.join("cache/uv");
    fs::create_dir_all(&uv_cache)?;
    set_startup_status(
        window,
        "python",
        "正在准备本地运行环境",
        "首次启动会下载 Python 及锁定依赖；不需要另外安装 Python、uv、Node 或打开终端。",
    );
    append_log(
        &setup_log,
        "Starting pinned uv sync for the packaged application.",
    );

    let mut sync = Command::new(&paths.uv);
    sync.args([
        "sync",
        "--locked",
        "--python",
        "3.11",
        "--no-dev",
        "--no-editable",
        "--link-mode",
        "copy",
    ])
    .arg("--project")
    .arg(&paths.project_root)
    .current_dir(&paths.project_root)
    .env(
        "UV_PROJECT_ENVIRONMENT",
        paths.pythonw.parent().unwrap().parent().unwrap(),
    )
    .env("UV_PYTHON_INSTALL_DIR", paths.app_home.join("python"))
    .env("UV_MANAGED_PYTHON", "1")
    .env("UV_CACHE_DIR", &uv_cache)
    .env("UV_NO_PROGRESS", "1");
    run_hidden_and_log(sync, &setup_log, Duration::from_secs(20 * 60))?;

    if !paths.pythonw.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "Python 运行环境准备完成后未找到 pythonw.exe。",
        ));
    }
    let mut smoke_test = Command::new(&paths.pythonw);
    smoke_test
        .args(["-c", "import pkas, fastapi, uvicorn"])
        .current_dir(&paths.project_root)
        .env("PYTHONPATH", source_import_root(&paths.project_root));
    run_hidden_and_log(smoke_test, &setup_log, Duration::from_secs(60))?;

    // A dedicated uv cache is disposable once the locked environment is healthy.
    let mut clean = Command::new(&paths.uv);
    clean
        .arg("cache")
        .arg("clean")
        .current_dir(&paths.project_root)
        .env("UV_CACHE_DIR", &uv_cache)
        .env("UV_NO_PROGRESS", "1");
    let _ = run_hidden_and_log(clean, &setup_log, Duration::from_secs(120));

    fs::write(&applied_manifest, manifest_fingerprint)?;
    append_log(&setup_log, "Pinned Python environment verified and ready.");
    Ok(())
}

fn start_qdrant_via_backend<R: Runtime>(
    paths: &RuntimePaths,
    window: &WebviewWindow<R>,
) -> std::io::Result<()> {
    if qdrant_ready() {
        set_startup_status(
            window,
            "vector",
            "检测到已有向量服务",
            "复用健康的本机 Qdrant；不会接管或关闭其他程序启动的服务。",
        );
        return Ok(());
    }
    if port_occupied(QDRANT_HTTP_PORT) || port_occupied(QDRANT_GRPC_PORT) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::AddrInUse,
            "Qdrant 默认端口 6333/6334 被非 Qdrant 服务占用；没有关闭或接管该进程。",
        ));
    }
    let bundled_qdrant = paths.project_root.join("runtime/qdrant/qdrant.exe");
    let development_qdrant = paths
        .project_root
        .join("runtime/tauri-payload/qdrant/qdrant.exe");
    if !bundled_qdrant.is_file() && !development_qdrant.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "安装包缺少 Qdrant 向量服务。",
        ));
    }

    set_startup_status(
        window,
        "vector",
        "正在启动本机向量服务",
        "向量数据单独保存在本用户的数据目录，只监听本机回环地址。",
    );
    let response = http_request(
        PKAS_PORT,
        "POST",
        "/api/runtime/services/qdrant",
        r#"{"action":"start","confirm_cloud":false}"#,
    )
    .ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::ConnectionAborted,
            "无法通过本机运行中心启动 Qdrant。",
        )
    })?;
    let status = response
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .unwrap_or_default();
    if !(200..300).contains(&status) {
        append_log(
            &paths.app_home.join("runtime/setup.log"),
            &format!("Runtime Center rejected Qdrant start request (HTTP {status})."),
        );
        return Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            "本机向量服务启动失败；可在运行中心查看错误和日志。",
        ));
    }
    for _ in 0..60 {
        if qdrant_ready() {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    Err(std::io::Error::new(
        std::io::ErrorKind::TimedOut,
        "Qdrant 未能在 30 秒内通过本机健康检查。",
    ))
}

fn start_backend(paths: &RuntimePaths) -> std::io::Result<Child> {
    if paths.packaged {
        let _ = fs::create_dir_all(paths.data_root.join("runtime"));
    } else {
        remember_project_root(&paths.project_root)?;
    }
    let logs = paths.data_root.join("runtime");
    fs::create_dir_all(&logs)?;
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("api-host.log"))?;
    let mut command = Command::new(&paths.pythonw);
    let port = PKAS_PORT.to_string();
    command
        .args([
            "-m",
            "pkas.cli",
            "serve",
            "--host",
            PKAS_HOST,
            "--port",
            port.as_str(),
        ])
        .current_dir(&paths.project_root)
        // The managed virtual environment carries third-party dependencies,
        // while the application package itself must always come from the
        // installed release. This avoids serving a previous wheel after an
        // ordinary source-only desktop update.
        .env("PYTHONPATH", source_import_root(&paths.project_root))
        .env("PKAS_PROJECT_ROOT", &paths.project_root)
        .env("PKAS_DATA_ROOT", &paths.data_root)
        .env("PKAS_RUNTIME_ROOT", paths.app_home.join("runtime"))
        .env("PKAS_ENV_FILE", paths.data_root.join("config/.env"))
        .env(
            "PKAS_QDRANT_URL",
            format!("http://{PKAS_HOST}:{QDRANT_HTTP_PORT}"),
        )
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    hide_console(&mut command);
    command.spawn()
}

fn startup_failure_message(error: &std::io::Error) -> (&'static str, &'static str) {
    match error.kind() {
        std::io::ErrorKind::NotFound => (
            "缺少必要的本地运行文件",
            "请重新运行知域安装包修复安装；没有扫描或修改你的个人文件。",
        ),
        std::io::ErrorKind::InvalidInput => (
            "发现多份已有知识库",
            "为避免选错资料，知域没有自动选库或改写数据；请先确认要使用的数据位置。",
        ),
        std::io::ErrorKind::AlreadyExists => (
            "已有项目根配置冲突",
            "知域未覆盖现有路径配置。请从原程序入口启动，或先核对当前用户已有的项目根设置。",
        ),
        std::io::ErrorKind::NotConnected => (
            "知识库数据盘暂不可用",
            "请连接首次安装时选择的数据盘后重试。若数据已迁移，请检查当前 Windows 用户目录下的 Zhishu\\storage-root.txt。",
        ),
        std::io::ErrorKind::PermissionDenied => (
            "当前 Windows 用户没有写入权限",
            "请确认当前知识库数据位置可写后，从托盘完全退出并重试。",
        ),
        std::io::ErrorKind::AddrInUse => (
            "本机服务端口发生冲突",
            "知域没有关闭或接管冲突程序。关闭占用 6333/6334/8765 的应用后重新打开。",
        ),
        std::io::ErrorKind::TimedOut => (
            "本地初始化或健康检查超时",
            "首次准备依赖需要网络；请检查网络后从托盘完全退出再打开。",
        ),
        _ => (
            "知域未能完成本机启动",
            "检查网络或修复安装后重试。",
        ),
    }
}

fn show_startup_error<R: Runtime>(
    window: &WebviewWindow<R>,
    paths: Option<&RuntimePaths>,
    error: &std::io::Error,
) {
    let (title, detail) = startup_failure_message(error);
    if let Some(paths) = paths {
        let log = paths.app_home.join("runtime/setup.log");
        let combined = format!("{detail} 日志：{}", log.display());
        set_startup_error(window, title, &combined);
    } else {
        set_startup_error(window, title, detail);
    }
}

fn start_application<R: Runtime>(app: &AppHandle<R>, window: &WebviewWindow<R>) {
    set_startup_status(
        window,
        "checking",
        "正在检查应用组件",
        "只检查知域本身；不会自动扫描磁盘、导入聊天记录或修改 Codex 配置。",
    );
    let paths = match runtime_paths(app) {
        Ok(paths) => paths,
        Err(error) => {
            show_startup_error(window, None, &error);
            return;
        }
    };
    if let Err(error) = fs::create_dir_all(&paths.data_root) {
        show_startup_error(window, Some(&paths), &error);
        return;
    }
    if let Err(error) = ensure_python_environment(&paths, window) {
        append_log(
            &paths.app_home.join("runtime/setup.log"),
            &format!("Python environment setup failed: {error}"),
        );
        show_startup_error(window, Some(&paths), &error);
        return;
    }
    set_startup_status(
        window,
        "service",
        "正在启动知识服务",
        "SQLite 和全文检索服务正在进行健康检查。",
    );
    let mut backend = match start_backend(&paths) {
        Ok(child) => child,
        Err(error) => {
            append_log(
                &paths.app_home.join("runtime/setup.log"),
                &format!("Knowledge service failed to start: {error}"),
            );
            show_startup_error(window, Some(&paths), &error);
            return;
        }
    };
    for attempt in 0..120 {
        if backend_ready() {
            set_startup_status(
                window,
                "vector",
                "正在连接本机向量服务",
                "通过运行中心启动或复用本机 Qdrant；不会接管其他程序的服务。",
            );
            if let Err(error) = start_qdrant_via_backend(&paths, window) {
                append_log(
                    &paths.app_home.join("runtime/setup.log"),
                    &format!("Qdrant startup failed: {error}"),
                );
                show_startup_error(window, Some(&paths), &error);
                return;
            }
            set_startup_status(
                window,
                "ready",
                "本机知识服务已就绪",
                "首次使用时再选择资料范围；程序不会在后台擅自扫盘或同步微信。",
            );
            std::thread::sleep(Duration::from_millis(450));
            let _ = window.navigate(format!("http://{PKAS_HOST}:{PKAS_PORT}/").parse().unwrap());
            return;
        }
        if let Ok(Some(_)) = backend.try_wait() {
            let error =
                std::io::Error::new(std::io::ErrorKind::Other, "本地知识服务启动后提前退出。");
            append_log(
                &paths.app_home.join("runtime/setup.log"),
                "Knowledge API exited before passing the readiness check.",
            );
            show_startup_error(window, Some(&paths), &error);
            return;
        }
        if attempt == 20 {
            set_startup_status(
                window,
                "service",
                "正在加载本地索引",
                "已有索引会直接复用；不会因为打开窗口而重扫全部文件。",
            );
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    let error = std::io::Error::new(
        std::io::ErrorKind::TimedOut,
        "后台未能在一分钟内通过本机健康检查。",
    );
    append_log(
        &paths.app_home.join("runtime/setup.log"),
        "Knowledge API did not become ready within 60 seconds.",
    );
    show_startup_error(window, Some(&paths), &error);
}

fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn hide_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW_LABEL) {
        let _ = window.hide();
    }
}

fn main() {
    tauri::Builder::default()
        // Register single-instance handling before setup to avoid duplicate runtimes.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            if args.iter().any(|argument| argument == "--quit") {
                app.exit(0);
                return;
            }
            show_main_window(app);
        }))
        .setup(|app| {
            let main_window = WebviewWindowBuilder::new(
                app,
                MAIN_WINDOW_LABEL,
                WebviewUrl::App("startup.html".into()),
            )
            .title("知域 · 个人知识系统")
            .decorations(false)
            .resizable(true)
            .inner_size(1440.0, 960.0)
            .min_inner_size(1080.0, 720.0)
            .center()
            .build()?;

            let status = MenuItem::with_id(app, "status", "知域 · 本机知识系统", false, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "打开知域", true, None::<&str>)?;
            let hide = MenuItem::with_id(app, "hide", "隐藏窗口", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出并停止本机服务", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&status, &separator, &open, &hide, &quit])?;

            TrayIconBuilder::new()
                .icon(tray_icon())
                .tooltip("知域 · 个人知识系统")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main_window(app),
                    "hide" => hide_main_window(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                })
                .build(app)?;

            // Do not claim or spawn a second API server when the app already exists.
            if backend_ready() {
                let _ = main_window.eval("window.setStartupError({title:'检测到已有后台服务',detail:'当前窗口不会接管或关闭旧后台。请先从原启动入口正常退出，再打开知域。'});");
                return Ok(());
            }
            if backend_port_occupied() {
                let _ = main_window.eval("window.setStartupError({title:'本机服务端口被占用',detail:'知域没有启动后台，也没有关闭其他程序。释放 8765 端口后重新打开。'});");
                return Ok(());
            }
            bind_backend_lifetime()?;
            let app_handle = app.handle().clone();
            std::thread::spawn(move || start_application(&app_handle, &main_window));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run the Zhishu desktop shell");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn volume(root: &str, free_bytes: u64, fixed: bool) -> DataVolume {
        DataVolume {
            root: PathBuf::from(root),
            free_bytes,
            fixed,
        }
    }

    #[test]
    fn storage_uses_roomiest_non_system_fixed_drive_with_capacity() {
        let local_home = PathBuf::from(r"C:\Users\tester\AppData\Local\Zhishu");
        let selected = choose_storage_home(
            &local_home,
            "C:",
            &[
                volume(r"C:\", 500 * 1024 * 1024 * 1024, true),
                volume(r"D:\", 20 * 1024 * 1024 * 1024, true),
                volume(r"G:\", 40 * 1024 * 1024 * 1024, true),
                volume(r"H:\", 90 * 1024 * 1024 * 1024, false),
            ],
        );

        assert_eq!(selected, PathBuf::from(r"G:\Zhishu"));
    }

    #[test]
    fn storage_falls_back_to_local_app_data_when_no_safe_data_drive_exists() {
        let local_home = PathBuf::from(r"C:\Users\tester\AppData\Local\Zhishu");
        let selected = choose_storage_home(
            &local_home,
            "C:",
            &[
                volume(r"C:\", 500 * 1024 * 1024 * 1024, true),
                volume(r"D:\", MIN_NON_SYSTEM_DATA_FREE_BYTES - 1, true),
                volume(r"H:\", 90 * 1024 * 1024 * 1024, false),
            ],
        );

        assert_eq!(selected, local_home);
    }

    #[test]
    fn existing_project_data_stays_in_place_while_runtime_uses_managed_storage() {
        let local_home = PathBuf::from(r"C:\Users\tester\AppData\Local\Zhishu");
        let existing_data = PathBuf::from(r"X:\LegacyKnowledge\data");
        let locations = choose_storage_locations(
            &local_home,
            "C:",
            &[
                volume(r"C:\", 500 * 1024 * 1024 * 1024, true),
                volume(r"G:\", 40 * 1024 * 1024 * 1024, true),
            ],
            false,
            Some(existing_data.clone()),
        )
        .unwrap();

        assert_eq!(locations.app_home, PathBuf::from(r"G:\Zhishu"));
        assert_eq!(locations.data_root, existing_data);
    }

    #[test]
    fn conflicting_legacy_data_locations_stop_before_selection() {
        let local_home = PathBuf::from(r"C:\Users\tester\AppData\Local\Zhishu");
        let result = choose_storage_locations(
            &local_home,
            "C:",
            &[],
            true,
            Some(PathBuf::from(r"X:\LegacyKnowledge\data")),
        );

        assert_eq!(result.unwrap_err().kind(), std::io::ErrorKind::InvalidInput);
    }

    #[test]
    fn disconnected_data_drive_has_a_distinct_actionable_startup_message() {
        let error = std::io::Error::new(std::io::ErrorKind::NotConnected, "offline");
        let (title, detail) = startup_failure_message(&error);

        assert_eq!(title, "知识库数据盘暂不可用");
        assert!(detail.contains("storage-root.txt"));
    }

    #[test]
    fn python_environment_fingerprint_tracks_dependencies_not_source_code() {
        let original = r#"{"python_version":"3.11","uv_version":"0.12","uv_sha256":"uv-a","uv_lock_sha256":"package-v1","dependency_lock_sha256":"deps-a","source_sha256":"source-a"}"#;
        let application_update = r#"{"python_version":"3.11","uv_version":"0.12","uv_sha256":"uv-a","uv_lock_sha256":"package-v2","dependency_lock_sha256":"deps-a","source_sha256":"source-b"}"#;
        let dependency_update = r#"{"python_version":"3.11","uv_version":"0.12","uv_sha256":"uv-a","uv_lock_sha256":"package-v3","dependency_lock_sha256":"deps-b","source_sha256":"source-b"}"#;
        let legacy_manifest = r#"{"python_version":"3.11","uv_version":"0.12","uv_sha256":"uv-a","uv_lock_sha256":"legacy-lock","source_sha256":"source-a"}"#;

        assert_eq!(
            python_environment_fingerprint(original),
            python_environment_fingerprint(application_update)
        );
        assert_ne!(
            python_environment_fingerprint(original),
            python_environment_fingerprint(dependency_update)
        );
        assert!(python_environment_fingerprint(legacy_manifest).is_some());
    }

    #[test]
    fn backend_imports_pkas_from_the_packaged_release_source() {
        assert_eq!(
            source_import_root(Path::new(r"C:\Program Files\Zhishu\pkas-app")),
            PathBuf::from(r"C:\Program Files\Zhishu\pkas-app\src")
        );
    }
}
