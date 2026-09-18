# 知枢桌面端

该目录承载 PKAS 的正式桌面外壳。当前纵向切片使用 Tauri 2 创建原生窗口和系统托盘，页面复用 `web/dist`，业务服务仍由现有 Python/FastAPI 内核提供。

## 当前边界

- 桌面壳只负责窗口、托盘、单实例和退出。
- 当前开发版连接 `http://127.0.0.1:8765/`，不会自行注册计划任务或开机启动。
- 当前不会启动 WeFlow、MCP Provider、知识同步或备份。
- 在 Tauri 完成等价生命周期验收前，`PKAS-Launcher.vbs` 仍是本机后台服务宿主。

## 本机编译

Rust 工具链保存在项目 `runtime` 目录。使用 `scripts/build_tauri_desktop.ps1` 编译，构建输出保存在 `desktop/src-tauri/target`。
