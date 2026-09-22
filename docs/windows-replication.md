# 知域 Windows 安装与恢复

## 普通用户：正式一键安装包（推荐）

不要把源码目录或单个 EXE 当成安装包发给用户。构建正式 NSIS 安装包：

```powershell
$repoRoot = (Resolve-Path .).Path
& "$repoRoot\scripts\build_tauri_desktop.ps1" -Mode Installer
```

输出位于构建数据盘的 `tauri-target\release\bundle\nsis` 目录（具体文件名和版本以构建结果为准）。安装器按当前 Windows 用户安装，不需要管理员权限；WebView2 不存在时安装器会静默安装引导程序。

客户只需安装并打开。首次启动时应用自动准备锁定的 Python 3.11 环境、依赖、本机 Qdrant 和知识服务；进度在知域窗口中显示，后台不弹终端。首次启动需要网络下载 Python 与依赖，可能需要数分钟；之后启动会复用已准备的运行环境，不要求客户安装 Python、uv、Node.js、npm、Rust 或 Qdrant。

安装目录与用户数据隔离：程序安装在当前用户的程序目录。首次启动会选择空间最大的非系统固定磁盘（至少剩余 10 GiB），新用户的索引数据库、向量数据、用户配置、运行日志与 Python 环境分别放在 `<磁盘>\Zhishu\data` 和 `<磁盘>\Zhishu\runtime`；`%LOCALAPPDATA%\Zhishu\storage-root.txt` 只保存两行位置指针（运行目录、数据目录）。没有合格数据盘时回退到 `%LOCALAPPDATA%\Zhishu`，资料流程会分别显示知识库与运行环境的实际位置，并标出系统盘占用。若 `%APPDATA%\Zhishu\pkas-root.txt` 指向旧版项目且该项目的 `data\index\pkas.sqlite` 是非空文件，新版会继续使用原 `data` 目录，不复制或迁移已有资料；如果旧版用户目录和旧项目目录都存在非运行时资料，程序会停止并明确提示，不擅自猜选。所选数据盘缺失时也会停止启动，不切换到空库。更新/卸载不主动删除数据目录。第一次启动不会扫盘、导入微信、配置 API 密钥或修改 Codex/MCP；首页进入资料接入后，首次点击“一键开始整理”才保存推荐方案并启动当前展示范围的目录索引。之后按分类选择全文、摘要或向量处理。Luna/DeepSeek、Embedding 等外部能力只在用户配置并触发后使用。聊天是可选的文件接入：普通用户只需导入自己合法导出的 XLSX 或 ChatLab 兼容 JSON；旧 WeFlow 同步仅保留为已存在本机环境的手动兼容模式。

`config/desktop_runtime.json` 固定 Python、uv、Qdrant 和 Node.js 版本及官方发布包 SHA-256，并固定 Everything 可执行文件校验值。安装包把运行所需的 Node/npm、Everything、Qdrant、uv、知域 Python 模块和已构建网页打入安装包；其中可保留一个只用于本机旧环境兼容的导出配置辅助脚本，但**不包含** WeFlow、微信数据库、聊天记录、API 密钥、虚拟环境、Git 历史、个人资料或第三方聊天导出器。首轮运行用校验过的 uv 和 uv 管理的 Python，在每位 Windows 用户自己的目录构建隔离环境。外机安装器生成不代表完成外机实装验收。

聊天资料是可选的文件接入：对方应自行安装并合法使用自己的导出工具，再把已导出的 WeFlow XLSX 或符合[ChatLab 微信文件约定 v1](chatlab-wechat-format-v1.md) 的 JSON 交给知域的“只读检查 → 确认导入”流程。旧 WeFlow 本机兼容同步不属于普通安装包交付承诺，也不会在陌生电脑上自动配置或启动。详情见[资料接入适配器契约](import-provider-contract.md)。

**验收边界：**成功生成 NSIS 文件只证明构建通过，不等于另一台实体 Windows 电脑已完成安装验收。第一次真实外机验收还要检查安装器启动、WebView2、依赖下载、服务健康、数据位置、搜索以及托盘退出后知域拥有的后台进程均已停止。此版本未签名时，Windows SmartScreen 仍可能提示“未知发布者”。

## 开发者/旧版：源码复制安装（不推荐给普通用户）

这条链路用于把 PKAS 程序复制到另一台 Windows 电脑，而不是复制当前用户的知识内容。导出包只复制干净 Git 工作区中的受版本控制源码；本机 `HANDOFF.md`、缓存、编译产物、`data/`、SQLite、Qdrant 数据、聊天、运行日志、`.env`、DPAPI 密钥、虚拟环境、Node 依赖和运行时二进制都不会进入包内。若工作区有未提交改动，脚本默认拒绝导出，避免“源码包版本”和清单的提交号不一致。

## 1. 在源电脑导出无个人数据的软件包

目标目录必须为空，脚本不会覆盖已有目录：

```powershell
$repoRoot = (Resolve-Path .).Path
$packageRoot = Join-Path $env:USERPROFILE 'Downloads\Zhishu-source'
& "$repoRoot\scripts\export_windows_package.ps1" `
  -ProjectRoot $repoRoot `
  -Destination $packageRoot
```

导出目录会生成 `PACKAGE_MANIFEST.json`，记录每个源码文件的 SHA-256，并明确标记数据库、密钥和个人数据未被复制。把整个 `$packageRoot` 目录复制到目标电脑即可。

## 2. 在目标电脑预检

要求 Windows x64、PowerShell 5.1+、`uv`、Node.js 20+ 和 npm。端口默认使用 Qdrant 6333/6334、管理台 8765：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  D:\PKAS\scripts\preflight_windows.ps1 `
  -ProjectRoot D:\PKAS
```

任一必要条件不满足时脚本返回非零，不会开始安装。

## 3. 安装并做隔离验收

第一次先不注册开机任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  D:\PKAS\scripts\bootstrap_windows.ps1 `
  -ProjectRoot D:\PKAS `
  -DataRoot D:\PKAS-data `
  -StartServices
```

安装器执行以下固定链路：

1. 运行预检；
2. 按 `uv.lock` 安装 Python 3.11 非 editable wheel；
3. 按 `package-lock.json` 构建管理台；
4. 从 Qdrant 官方 GitHub Release 下载固定的 1.19.0 Windows 包并核对 SHA-256；
5. 在全新数据目录初始化当前 schema（现为 18）；
6. 在禁用 Embedding、Rerank、Agent 的条件下导入合成探针并验证 FTS；
7. 启动只监听 `127.0.0.1` 的 Qdrant 并做健康检查。

验收报告位于：

- `<DataRoot>\runs\windows-bootstrap.json`
- `<DataRoot>\runs\replica-verification.json`

该过程不配置 DeepSeek、Embedding 或 Codex MCP，也不会从源电脑复制密钥。目标电脑必须由其当前 Windows 用户单独配置 API 凭据。

## 4. 验收后注册当前用户任务

确认端口、数据目录和管理台无误后，再加 `-RegisterTasks`。WeFlow 是可选模块，只有明确提供现存目录才注册同步任务；同步任务默认每天 23:00 运行一次，可用 `-DailyAt '22:30'` 修改时间：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  D:\PKAS\scripts\bootstrap_windows.ps1 `
  -ProjectRoot D:\PKAS `
  -DataRoot D:\PKAS-data `
  -StartServices `
  -RegisterTasks `
  -WeFlowRoot 'D:\WeFlow' `
  -DailyAt '23:00'
```

可选任务为 `PKAS-Qdrant`、`PKAS-Core-Worker`、`PKAS-Dashboard`、`PKAS-Daily-Backup` 和 `PKAS-Knowledge-Sync`。策略文件缺失、损坏、版本不支持或 `autostart_enabled` 未显式设为 `true` 时，安装器会 fail closed，不能注册任何任务。获得目标电脑用户单独授权后，备份任务可每日 03:00 生成不含密钥的恢复包并默认只保留最新三版；`PKAS-Knowledge-Sync` 还需要传入 `-WeFlowRoot`，并且只有 `weflow-daily-import.json` 明确启用时才会在后台启动 WeFlow。所有服务只绑定本机地址；MCP 不会被安装器启用。

恢复包不属于无数据安装包。需要迁移个人资料时，应单独复制一个已经通过 `scripts/verify_restore.py` 验证的恢复包，并在目标电脑恢复到空数据目录。恢复后同步根默认关闭、Qdrant 必须重建、API 密钥必须由目标 Windows 用户重新配置。

## 5. 精确卸载运行任务

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  D:\PKAS\scripts\uninstall_windows_runtime.ps1 `
  -ProjectRoot D:\PKAS `
  -DataRoot D:\PKAS-data `
  -StopQdrant
```

卸载器只移除工作目录属于本安装的任务，并只结束本安装 `.venv` 下的 PKAS 允许模块及 PID 文件对应的 Qdrant。它不删除数据目录、源码、虚拟环境或运行时文件，便于恢复和人工核对。

## 当前验收边界

本仓库已在独立本机路径完成源码导出、安装、schema/FTS/Qdrant 检查、任务注册、服务启动、精确卸载和数据保留的自动化验证。另一台物理 Windows 电脑尚未实演，因此“跨机器复制已正式验收”仍不能成立。
