#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};

const MAIN_WINDOW_LABEL: &str = "main";
const PKAS_DASHBOARD_URL: &str = "http://127.0.0.1:8765/";

fn valid_project_root(root: &std::path::Path) -> bool {
    root.join("src/pkas/api.py").is_file() && root.join(".venv/Scripts/pythonw.exe").is_file()
}

fn root_config_path() -> Option<std::path::PathBuf> {
    std::env::var_os("APPDATA").map(|appdata| {
        std::path::PathBuf::from(appdata).join("Zhishu").join("pkas-root.txt")
    })
}

fn configured_project_root() -> Option<std::path::PathBuf> {
    let raw = std::env::var_os("PKAS_PROJECT_ROOT")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            root_config_path()
                .and_then(|path| std::fs::read_to_string(path).ok())
                .map(|value| std::path::PathBuf::from(value.trim()))
        })?;
    raw.canonicalize().ok().filter(|path| valid_project_root(path))
}

fn executable_project_root() -> std::io::Result<Option<std::path::PathBuf>> {
    let executable = std::env::current_exe()?;
    Ok(executable
        .ancestors()
        .find(|path| valid_project_root(path))
        .map(std::path::Path::to_path_buf))
}

fn project_root() -> std::io::Result<std::path::PathBuf> {
    // A copied/new desktop executable must manage the project that contains
    // it.  A global root file is only a fallback for an installed shell that
    // intentionally lives outside its data project; otherwise an obsolete
    // desktop version can silently route a new EXE to the wrong backend.
    if let Some(root) = executable_project_root()? {
        return Ok(root);
    }
    if let Some(root) = configured_project_root() {
        return Ok(root);
    }
    Err(
        std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "未找到知识库目录：请通过知枢安装入口启动，或重新安装本机配置",
        )
    )
}

fn remember_project_root(root: &std::path::Path) -> std::io::Result<()> {
    let Some(path) = root_config_path() else { return Ok(()); };
    std::fs::create_dir_all(path.parent().expect("config path has parent"))?;
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, root.to_string_lossy().as_bytes())?;
    std::fs::rename(temporary, path)
}

// Keep the desktop and every backend descendant in one OS-owned lifetime.
// The non-inheritable handle is deliberately held until process teardown:
// even a crash closes it and Windows terminates the remaining descendants.
#[cfg(target_os = "windows")]
fn bind_backend_lifetime() -> std::io::Result<()> {
    use windows_sys::Win32::{Foundation::CloseHandle, System::{
        JobObjects::{CreateJobObjectW, SetInformationJobObject, AssignProcessToJobObject,
            JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE},
        Threading::GetCurrentProcess,
    }};
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() { return Err(std::io::Error::last_os_error()); }
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(job, JobObjectExtendedLimitInformation,
            &limits as *const _ as *const _, std::mem::size_of_val(&limits) as u32) == 0
            || AssignProcessToJobObject(job, GetCurrentProcess()) == 0 {
            let error = std::io::Error::last_os_error();
            CloseHandle(job);
            return Err(error);
        }
    }
    Ok(())
}

#[cfg(not(target_os = "windows"))]
fn bind_backend_lifetime() -> std::io::Result<()> {
    Err(std::io::Error::new(std::io::ErrorKind::Unsupported, "backend ownership is Windows-only"))
}

fn tray_icon() -> tauri::image::Image<'static> {
    let mut pixels = vec![0u8; 32 * 32 * 4];
    for y in 0i32..32 { for x in 0i32..32 {
        let offset = ((y * 32 + x) * 4) as usize;
        let color = if (x - 16).abs() + (y - 16).abs() < 12 {
            [58, 211, 209, 255]
        } else { [20, 32, 44, 255] };
        pixels[offset..offset + 4].copy_from_slice(&color);
    }}
    tauri::image::Image::new_owned(pixels, 32, 32)
}

fn backend_ready() -> bool {
    use std::io::{Read, Write};
    let Ok(mut stream) = std::net::TcpStream::connect_timeout(
        &"127.0.0.1:8765".parse().unwrap(), std::time::Duration::from_millis(300),
    ) else { return false; };
    let _ = stream.set_read_timeout(Some(std::time::Duration::from_millis(500)));
    let _ = stream.write_all(b"GET /api/runtime/ping HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n");
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok() && response.contains("pkas-runtime")
}

fn backend_port_occupied() -> bool {
    std::net::TcpStream::connect_timeout(
        &"127.0.0.1:8765".parse().unwrap(),
        std::time::Duration::from_millis(150),
    )
    .is_ok()
}

fn start_backend() -> std::io::Result<std::process::Child> {
    let root = project_root()?;
    remember_project_root(&root)?;
    let logs = root.join("data/runtime");
    std::fs::create_dir_all(&logs)?;
    let log = std::fs::OpenOptions::new().create(true).append(true).open(logs.join("api-host.log"))?;
    let mut command = std::process::Command::new(root.join(".venv/Scripts/pythonw.exe"));
    command.args(["-m", "pkas.cli", "serve", "--host", "127.0.0.1", "--port", "8765"])
        .current_dir(&root).stdout(log.try_clone()?).stderr(log);
    #[cfg(target_os = "windows")]
    { use std::os::windows::process::CommandExt; command.creation_flags(0x08000000); }
    command.spawn()
}

fn backend_start_failure_message(error: &std::io::Error) -> &'static str {
    match error.kind() {
        std::io::ErrorKind::NotFound => {
            "未找到完整的本地知识库运行环境。请从知枢安装入口重新打开，或修复安装目录后重试。"
        }
        std::io::ErrorKind::PermissionDenied => {
            "当前用户没有知识库目录的读写权限。请检查安装目录权限后重新打开。"
        }
        _ => "本地知识服务无法启动。请从托盘退出知枢后重试；退出会停止本次启动的后台。",
    }
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
        // The single-instance plugin must be registered first so a second launch
        // is routed to the existing desktop process instead of duplicating it.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            if args.iter().any(|argument| argument == "--quit") {
                app.exit(0);
                return;
            }
            show_main_window(app);
        }))
        .setup(|app| {
            let main_window = WebviewWindowBuilder::new(app, MAIN_WINDOW_LABEL, WebviewUrl::App("startup.html".into()))
                .title("知枢 · 个人 AI 控制中心")
                .decorations(false)
                .resizable(true)
                .inner_size(1440.0, 960.0)
                .min_inner_size(1080.0, 720.0)
                .center()
                .build()?;

            let status =
                MenuItem::with_id(app, "status", "知枢 · 本机知识中枢", false, None::<&str>)?;
            let open = MenuItem::with_id(app, "open", "打开控制中心", true, None::<&str>)?;
            let hide = MenuItem::with_id(app, "hide", "隐藏窗口", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出知枢并停止后台", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&status, &separator, &open, &hide, &quit])?;

            TrayIconBuilder::new()
                .icon(tray_icon())
                .tooltip("知枢 · 个人 AI 控制中心")
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

            // Never start a backend before the tray has been created successfully.
            if backend_ready() {
                let _ = main_window.eval("document.getElementById('status').textContent='检测到旧版独立后台：本窗口不能管理它的退出。请先正常停止旧后台，再重新打开知枢。新版不会额外启动后台。'");
                return Ok(());
            }
            if backend_port_occupied() {
                let _ = main_window.eval("document.getElementById('status').textContent='本机 8765 端口正被其他程序占用，知枢没有启动后台。请释放该端口后重新打开。'");
                return Ok(());
            }
            bind_backend_lifetime()?;
            std::thread::spawn(move || {
                let _ = main_window.eval(
                    "document.getElementById('status').textContent='正在启动本地知识服务，不会同步资料或打开终端窗口。'",
                );
                let mut backend = match start_backend() {
                    Ok(child) => child,
                    Err(error) => {
                        let message = backend_start_failure_message(&error);
                        let script = format!(
                            "document.getElementById('status').textContent={:?};",
                            message
                        );
                        let _ = main_window.eval(&script);
                        return;
                    }
                };
                for attempt in 0..60 {
                    if backend_ready() {
                        let _ = main_window.navigate(PKAS_DASHBOARD_URL.parse().unwrap());
                        return;
                    }
                    if let Ok(Some(_)) = backend.try_wait() {
                        let _ = main_window.eval(
                            "document.getElementById('status').textContent='本地知识服务启动后提前退出。请从托盘退出知枢后重试；若仍出现，请修复安装目录。'",
                        );
                        return;
                    }
                    if attempt == 8 {
                        let _ = main_window.eval(
                            "document.getElementById('status').textContent='正在加载本地索引与设置，资料不会被重新扫描。'",
                        );
                    } else if attempt == 24 {
                        let _ = main_window.eval(
                            "document.getElementById('status').textContent='正在进行本机健康检查；如超过一分钟会给出可操作提示。'",
                        );
                    }
                    std::thread::sleep(std::time::Duration::from_millis(500));
                }
                let _ = main_window.eval("document.getElementById('status').textContent='后台未就绪，请从托盘退出知枢后重试；退出会停止本次启动的后台。'");
            });

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
