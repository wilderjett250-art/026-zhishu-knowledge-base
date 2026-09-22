# 知域（Zhishu）

知域是一个**本地优先、可追溯、可选择处理深度**的个人知识系统。它不把整块硬盘或聊天记录直接交给模型，而是先让你看清电脑里有什么，再由你决定哪些资料只保留目录、哪些做全文检索、哪些进入语义向量检索。

桌面端、Codex MCP、本机 HTTP API 和 RPA 回环桥共用同一条检索链路：结果始终带来源和定位，而不是只返回一段无法核对的“答案”。

> 当前仓库提供 Windows 本地部署源码。个人资料、聊天导出、数据库、向量数据、运行日志与 API 凭据均不在 Git 中，也不会随源码复制。

## 适合做什么

- 盘点指定目录或已授权本地资料，建立轻量目录索引；
- 解析 Word、Excel、PDF、PPT、Markdown、文本、代码、JSON/JSONL、邮件等内容；
- 使用 SQLite FTS5 做本地全文检索，按需启用 Qdrant + Embedding 做语义与混合检索；
- 让 Codex 通过 MCP 先检索、再回答，并回到来源文件、文档或段落；
- 管理已授权的聊天导出文件（WeFlow XLSX 与 ChatLab 兼容微信 JSON），增量去重后检索历史沟通；
- 在桌面端查看资料范围、处理层级、失败原因、来源状态与 RAG 评测记录。

## 它如何组织资料

```text
本地文件 / Obsidian Vault / 已授权聊天导出
                    │
                    ▼
           A 库：目录索引（路径、类型、时间、分类）
                    │  选择处理深度
        ┌───────────┼───────────┬────────────┐
        ▼           ▼           ▼            ▼
       L0          L1          L2           L3
    仅目录     摘要/来源    正文 + FTS   正文 + FTS + 向量
                    │
                    ▼
       SQLite（正式记录与全文） + Qdrant（可重建向量）
                    │
                    ▼
     统一检索 → 桌面端 / Codex MCP / 本机 API / RPA 回环桥
```

- **L0**：只记录文件在哪里；不读取正文、不调用模型。
- **L1**：记录来源与有限摘要，回答“它大概是什么”。
- **L2**：本地解析正文并建立全文索引，可精确查文档内容。
- **L3**：在 L2 基础上构建向量，用于“意思相近”的语义检索。

原文件默认留在原处。只有经过确认的资料才会被解析或向量化；Qdrant 只是可重建的派生索引，不是事实源。

## 快速开始（源码方式）

适用于 Windows 10/11 x64。先安装 Git、Python 3.11+、Node.js 20+ 与 [uv](https://docs.astral.sh/uv/)。不配置 Embedding 也可以直接使用全文检索。

```powershell
git clone https://github.com/wilderjett250-art/026-zhishu-knowledge-base.git zhishu
Set-Location zhishu

uv sync --extra dev
Push-Location web
npm ci
npm run build
Pop-Location

uv run pkas init
uv run pkas serve --open-browser
```

管理台默认地址为 `http://127.0.0.1:8765`，只监听当前电脑。首次使用请在“资料接入”中确认范围；系统不会静默扫描磁盘、导入聊天、配置 API 密钥或修改 Codex 设置。

### Windows 桌面安装包

维护者可从源码构建当前用户安装版 NSIS 安装包；安装后用户不需要预装 Python、Node.js、uv、Rust 或 Qdrant。完整构建、复制安装、卸载和恢复说明见 [Windows 安装与恢复](docs/windows-replication.md)。

给同学或新电脑使用时，请从 [GitHub Releases](https://github.com/wilderjett250-art/026-zhishu-knowledge-base/releases/latest) 下载 `知域_*_x64-setup.exe` 并正常安装；不要复制单个 EXE，也不要复制其他人的 `data`、运行目录或聊天导出。安装包与个人数据分离，首次启动只准备本机运行环境，不会自动扫盘、导入聊天、配置模型密钥或启用 Codex/MCP。

当前 `main` 分支发布源码与构建脚本，不提交个人数据或安装产物。源码方式适合开发者；普通使用者优先使用上面的正式安装包。

## Codex / RPA 接入

- **Codex MCP**：运行项目虚拟环境中的 `python -m pkas.mcp_server`，将其作为 stdio MCP 注册到 Codex。工具包括检索、读文档、列来源、目录定位和经确认的资料导入。
- **RPA / 实在 Agent**：只能通过受限的 `127.0.0.1` HTTP 桥访问检索结果，不能直连 SQLite。桥使用独立 DPAPI 令牌，不开放 SQL、任意路径或原文下载。见 [RPA 本机桥](docs/rpa-loopback-bridge.md)。

## 隐私与安全边界

- `data/`、向量、数据库、聊天导出、日志、恢复包、`.env` 和 DPAPI 密钥均被 Git 忽略；
- 服务默认只绑定 `127.0.0.1`，不向局域网或公网暴露资料；
- 云端 Embedding、重排、文档视觉解析均为显式可选能力；`restricted` 资料默认不会被发送；
- 聊天导入采用可扩展的文件适配器：WeFlow 仅是已导出 XLSX 的兼容来源之一；不读取微信解密密钥、不直接访问 WCDB、不自动发送消息，也不随安装包分发第三方导出器；
- 每个检索结果保留来源、时间、隐私级别和处理状态；模型推断不能替代原始证据。

## 开发与验证

```powershell
uv run ruff check src tests
uv run pyright
uv run python -m pytest

Push-Location web
npm run build
Pop-Location
```

更多设计细节：

- [核心架构](docs/architecture.md)
- [数据模型](docs/data-model.md)
- [检索评测协议](docs/RAG_EVALUATION.md)
- [Windows 安装与恢复](docs/windows-replication.md)
- [资料接入适配器契约](docs/import-provider-contract.md)
- [ChatLab 微信文件约定 v1](docs/chatlab-wechat-format-v1.md)

## 授权

仓库目前是公开源码的技术预览，具体使用、再发布和商业授权以 [LICENSE.md](LICENSE.md) 为准。该文件当前不是 OSI 开源许可证；若要开放二次分发或商业使用，需要由维护者另行选择并发布明确许可证。
