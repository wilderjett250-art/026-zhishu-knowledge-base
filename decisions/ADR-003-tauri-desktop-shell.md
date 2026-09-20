# ADR-003：Tauri 2 作为正式桌面外壳

- Status: accepted
- Date: 2026-08-28

## Decision

- 正式客户端采用Tauri 2 + 现有React管理界面；Python/FastAPI、SQLite、Qdrant、Agent和工作流内核保持独立。
- Tauri只拥有窗口、托盘、单实例、后台生命周期、更新和安装体验，不复制业务逻辑。
- 当前VBS/Windows Forms宿主保留为试用和维修入口，直到Tauri真实EXE通过等价启动/停止验收。

## Consequences

- Windows和macOS可复用同一React界面；平台差异限制在Tauri/Rust适配层。
- 开发需要Rust MSVC工具链与WebView2。工具链和缓存固定在项目`runtime`下，避免新增C盘开发缓存。
- 未签名开发EXE只用于本机验收；商业分发仍需代码签名、安装器、升级与授权工作包。
