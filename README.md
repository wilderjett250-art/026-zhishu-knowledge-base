# 知枢：个人知识智能体系统

知枢（Personal Knowledge & Agent System，PKAS）是一个本地优先的个人智能系统。它把业务资料、工作经验、聊天导出、学习材料和个人偏好放进同一个逻辑知识库，让 Codex 在每个新任务中自行检索所需证据，同时保留工作流、智能体运行、自我画像和未来蒸馏数据。

它不是一个“把所有文件塞给模型”的大文件夹。资料原件、可检索索引、长期画像和训练候选互相分离，但可以沿来源关系追溯。

## 已有能力

- 明确路径的只读检查、敏感文件跳过、SHA-256 原件留存和内容去重。
- TXT、Markdown、代码、JSON/JSONL 聊天、CSV、HTML、PDF、DOCX、XLSX 解析。
- SQLite FTS5 中文全文检索、工作/自我/共享/蒸馏领域过滤和 restricted 隔离。
- 持期资料源目录：可先盘点现成文件，再按目录选择 catalog 或 index 增量同步。
- Codex 历史任务流式抽取和新任务完成后自动写回；不采集推理、工具输出或附件二进制。
- 检索结果到原文件、仓内原件、文档和段落定位的证据链。
- 可记录步骤、状态、输出和失败信息的资料导入与索引工作流。
- Codex MCP 工具：检索、读原文、列来源、检查/确认导入、准备智能体上下文、提交画像与蒸馏候选。
- WeFlow 导出记录发现、XLSX 会话选择、ChatLab `0.0.2` 兼容导入和消息去重。
- 微信客户档案、聊天时间线、客户消息检索、需求/承诺/待办审核和回复上下文。
- 自我画像人工批准机制，以及已批准蒸馏样本的标准 JSONL 导出。
- 一个本地管理台，统一操作知识、来源、工作流、智能体、自我画像、蒸馏和审计记录。

## 启动

环境要求：Python 3.11+、Node.js 20+、`uv` 和 npm。

```powershell
cd E:\codex-kb
uv sync --extra dev
cd web
npm install
npm run build
cd ..
uv run pkas serve --open-browser
```

管理台地址为 `http://127.0.0.1:8765`，API 文档为 `http://127.0.0.1:8765/api/docs`。服务默认只监听本机。

首次导入资料时，在“资料接入”中填写一个准确的绝对路径，先执行只读检查，再选择领域和隐私级别并确认导入。系统不会自行扫描整块磁盘，也不会自动导入任何个人资料。

## 现成数据与持续同步

“一个知识库”是统一的检索和权限入口，不是把所有原始文件复制成一个巨型数据库。系统提供两种持续资料源模式：

- `catalog`：保存文件路径、大小、修改时间和状态，让 Codex 先知道资料在哪里；不复制正文。
- `index`：对受支持且非敏感的文件抽取正文、保存哈希原件并建立检索索引；再次扫描只处理变化。

Codex 会话使用专用 `codex_sessions` 连接器。首次同步会流式读取历史 JSONL，只保留每轮的用户请求、最终回答、任务 ID、时间和工作目录；推理、工具调用输出、图片/音频二进制不进入知识库。之后由 Codex `agent-turn-complete` 通知即时写入新任务，同一 turn 重复通知不会重复入库，常见密钥会在落盘前脱敏。

已注册资料源可以通过 `uv run --no-sync pkas-sync-worker` 一次完成增量刷新。正式环境使用 Codex 本地自动化每日调用该入口，自动化只处理已经完成一次性授权的范围。

管理台“知识与来源”会显示每个持续资料源的有效项、已索引项、安全跳过项、不可读取项和最近同步时间；可以逐源执行增量刷新，也可以按文件名或路径片段检索资料地图。历史版本和 Codex 桌面内部的 ambient-suggestion 任务不会进入可检索结果。

## WeFlow 微信客户接入

先由 WeFlow 使用它已保存的数据库连接打开微信记录并导出 XLSX。知枢不读取 `decryptKey`、不直接打开 WCDB，也不依赖 WeFlow HTTP API；它只读取 WeFlow 自己维护的 `weflow-export-records.json`，再处理用户明确选择的现存导出文件。

管理台操作入口为“WeFlow / 资料”：

1. 点击“发现现存 XLSX 导出”。默认自动定位 `%APPDATA%\weflow\weflow-export-records.json`，也可以填写准确的绝对路径。
2. 系统只读列出仍然存在的导出文件；勾选确定属于业务客户的私聊或群聊。
3. 先检查 XLSX 表头、文件哈希、会话数量和声明消息数，再明确确认导入。
4. 原始 XLSX 保存为 SHA-256 快照，消息按会话、发送者、时间、类型与内容去重并建立客户全文索引。
5. 在“微信客户”中查看时间线、检索历史沟通、维护已确认客户档案、审核需求/承诺/待办，并为 Codex 准备回复依据。

ChatLab JSON 仍作为兼容入口保留。两种方式都不会直接操作微信或自动发送客户消息。

## 接入 Codex

MCP 服务启动命令：

```powershell
uv --directory E:\codex-kb run pkas-mcp
```

把该 stdio 命令注册为 Codex 的 MCP 服务后，Codex 可调用以下工具：

- `search_knowledge`：检索带来源的知识片段。
- `read_document`：按文档 ID 分页读取原文。
- `list_sources`：查看最近资料来源。
- `inspect_import_path`：只读检查用户给出的路径。
- `import_confirmed_path`：在用户明确批准后导入同一路径。
- `list_sync_roots`：查看持续资料源及同步状态。
- `register_sync_root`：在用户确认后注册目录或 Codex 历史会话源。
- `scan_sync_root`：增量盘点或索引已完成一次性授权的资料源。
- `search_source_catalog`：按文件名和路径定位尚未抽取的现成资料。
- `prepare_agent_context`：按任务选择领域并准备证据上下文。
- `save_persona_candidate`：提交待用户审核的个人观察。
- `save_distillation_candidate`：提交待用户审核的蒸馏样本。
- `list_weflow_customers`：列出明确同步过的微信客户会话。
- `get_customer_timeline`：读取授权客户的聊天时间线。
- `search_customer_messages`：检索需求、报价、进度和历史承诺。
- `prepare_customer_reply_context`：准备客户档案、聊天证据和工作知识。
- `save_customer_signal_candidate`：提交待审核的需求、承诺、待办或风险。

默认规则是：先检索再回答；restricted 资料不自动返回；导入必须先检查并明确确认；自我画像和蒸馏样本只能由智能体创建候选，最终批准权属于用户。

## 命令行

```powershell
uv run pkas init
uv run pkas inspect "E:\明确的资料目录"
uv run pkas ingest "E:\明确的资料目录" --domain work --privacy private --yes
uv run pkas search "项目的核心业务规则"
uv run pkas stats
uv run pkas weflow-exports
uv run pkas weflow-export-inspect wxid_customer
uv run pkas weflow-export-import wxid_customer --inspection-token <检查令牌> --yes
uv run pkas chatlab-inspect "E:\WeFlow导出\客户.json"
uv run pkas mcp
```

## 数据与隐私

- `data/raw/sha256`：授权导入的原始资料，按内容哈希保存。
- `data/raw/weflow-xlsx/sha256`：明确选择后导入的 WeFlow 原始 XLSX 哈希快照。
- `data/raw/weflow/sha256`：兼容导入的 ChatLab JSON 哈希快照。
- `data/index/pkas.sqlite`：元数据、全文索引、运行记录和审计记录。
- `data/distill/exports`：经过批准后导出的蒸馏 JSONL。
- 密码、令牌、私钥、证书和常见密钥文件名不会导入。
- Codex 自动写回只保存用户请求和最终回答，并在写入前执行常见凭证脱敏。
- 私人聊天和第三方内容建议标记为 `restricted`。
- 原始资料、数据库、运行记录和导出数据均被 Git 忽略。

当前本机实例已经接入用户明确授权的 Codex 历史、长期记忆和项目目录；这些真实索引与运行数据均保存在 Git 忽略的数据目录中。接入微信等聊天时，应使用用户合法取得的可读导出文件，并先检查样本字段与第三方隐私范围。

## 开发验证

```powershell
uv run ruff check src tests
uv run pyright
uv run pytest
cd web
npm run build
```

系统边界和组件关系见 `docs/architecture.md`，实体、状态与隐私字段见 `docs/data-model.md`。
