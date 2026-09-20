# 知枢桌面端

Windows 桌面版用 Tauri 提供正式安装、窗口与托盘；网页控制台复用本项目构建产物，Python/FastAPI 提供本机知识服务，Qdrant 作为本机向量服务。安装程序按当前用户安装，不要求管理员权限。

## 普通用户启动流程

1. 下载并运行 NSIS 安装包，按提示完成安装。
2. 第一次打开时，知枢在后台准备锁定的 Python 3.11 运行环境，再启动本机知识服务和 Qdrant；窗口显示进度与可读错误，所有子进程均不弹终端。
3. 首页进入“资料底座”后，顶部三步流程条持续显示本机索引、AI分类摘要、知识库入库进度。首次使用直接点“一键启动本地索引 · 推荐方案”，会保存“工作提效型”并扫描页面展示的默认范围；暂停或有警告时可续原任务。
4. 索引完成后从流程条进入 AI 整理，按批次处理并可中断续跑；结果可先看分类和 L0–L3 入库预览，再确认加入。之后在知枢内验证搜索，再从能力中心手动连接 Codex/其他兼容客户端。MCP 不会被静默写入。

“一键开始整理”仅建立目录与分类工作，不会把全部文件复制进知识库、不会自动导入微信，也不会改 Codex 设置。全文解析、AI 分类/摘要和向量化按所选策略执行；调用云端模型或 Embedding 前仍需配置相应服务。

第一次启动需要网络，用于下载锁定的 Python 3.11 与 Python 依赖，可能需要几分钟；完成后复用用户级环境。安装包已包含 uv、Qdrant、Everything 便携命令行资源和 WeFlow 同步所需 Node/npm 运行时，因此客户电脑不需要预装 Python、uv、Node.js、npm、Rust 或 Qdrant。WebView2 缺失时，Windows 安装器会静默运行引导程序。WeFlow 手动同步仍要求用户自行安装并登录 WeFlow；知枢不会自动打开它。

## 程序和用户数据

- 安装资源位于当前用户的程序安装目录。
- 新安装首次启动时会挑选剩余空间不少于 10 GiB、空间最大的非系统固定磁盘，数据、向量文件、用户配置和运行日志放在 `<磁盘>\Zhishu\data`，Python 环境与运行时日志放在 `<磁盘>\Zhishu\runtime`。`%LOCALAPPDATA%\Zhishu\storage-root.txt` 只保存两行位置指针：运行目录与数据目录；没有符合条件的磁盘时回退到 `%LOCALAPPDATA%\Zhishu`，界面会提示系统盘上实际使用的目录。
- 若发现旧版 `%LOCALAPPDATA%\Zhishu\data` 已有用户资料，首次升级会保留并继续使用；若 `%APPDATA%\Zhishu\pkas-root.txt` 指向旧版项目且其中存在非空的 PKAS 数据库文件，则新版继续使用该项目的 `data` 目录，不复制或迁移 5GB 级资料。两处都存在旧资料时会停止并提示，不猜测选库。数据盘之后暂不可用时，程序也会停止，不会悄悄新建空知识库。
- 卸载程序不会主动删除上述用户数据。升级与迁移前仍应使用项目恢复包，不复制原机器的密钥。
- 关闭窗口只会隐藏到托盘；从托盘选择“退出并停止本机服务”后，知枢拥有的后台进程由 Windows Job Object 一起停止。外部程序启动的服务不会被知枢强制结束。
- 默认仅监听 `127.0.0.1`。WeFlow、Codex MCP、计划任务和全盘扫描都不由安装器自动启用。

## 开发者构建

构建机需要本项目的 Rust 工具链、Node.js/npm 和联网能力；这些是构建依赖，不是用户的安装依赖。先准备 `web/node_modules`，然后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  E:\codex-kb\scripts\build_tauri_desktop.ps1 `
  -Mode Installer
```

构建脚本先构建网页，再按 `config/desktop_runtime.json` 下载并校验 uv、Qdrant 与 Node.js 官方发布包，并校验随仓库维护的 Everything 可执行文件，最后生成当前用户安装版。安装包和 Rust 构建缓存优先输出到 I 盘，缺少 I 盘时使用 G 盘。生成的包位于构建目录 `tauri-target\release\bundle\nsis`。
