# 知枢：个人知识智能体系统

知枢（Personal Knowledge & Agent System，PKAS）是一个本地优先的个人智能系统。它把业务资料、工作经验、聊天导出、学习材料和个人偏好放进同一个逻辑知识库，让 Codex 在每个新任务中自行检索所需证据，同时保留工作流、智能体运行、自我画像和未来蒸馏数据。

它不是一个“把所有文件塞给模型”的大文件夹。资料原件、可检索索引、长期画像和训练候选互相分离，但可以沿来源关系追溯。

## 已有能力

- 明确路径的只读检查、敏感文件跳过、SHA-256 原件留存和内容去重。
- TXT、Markdown、代码、JSON/JSONL 聊天、CSV、HTML、PDF、DOCX、XLSX 解析。
- SQLite FTS5 中文全文检索、工作/自我/共享/蒸馏领域过滤和 restricted 隔离。
- 检索结果到原文件、仓内原件、文档和段落定位的证据链。
- 可记录步骤、状态、输出和失败信息的资料导入与索引工作流。
- Codex MCP 工具：检索、读原文、列来源、检查/确认导入、准备智能体上下文、提交画像与蒸馏候选。
- WeFlow 本地 API 连接、会话选择、ChatLab `0.0.2` 离线导入和增量游标同步。
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

## WeFlow 微信客户接入

在 WeFlow 设置中先完成微信数据库连接，然后开启“API 服务”，配置 Access Token。知枢默认连接 `http://127.0.0.1:5031`，并且拒绝把 WeFlow 请求发送到非本机地址。

管理台操作入口为“WeFlow / 资料”：

1. 输入 WeFlow Access Token 并测试连接。Token 只停留在当前页面与本次后端请求中，不写数据库、日志、快照或 Git。
2. 只读加载会话列表，勾选确定属于业务客户的私聊或群聊。
3. 确认后做增量同步。聊天原始响应保存为 SHA-256 快照，消息按平台 ID、发送者、时间与内容去重。
4. 在“微信客户”中查看时间线、检索历史沟通、维护已确认客户档案、审核需求/承诺/待办，并为 Codex 准备回复依据。

如果不启用 WeFlow API，也可以导出 ChatLab JSON，在同一页面先检查文件结构和会话 ID，再作为 `restricted` 客户聊天导入。系统不会直接操作微信或自动发送客户消息。

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
uv run pkas weflow-check
uv run pkas weflow-sessions
uv run pkas weflow-sync wxid_customer --yes
uv run pkas chatlab-inspect "E:\WeFlow导出\客户.json"
uv run pkas mcp
```

## 数据与隐私

- `data/raw/sha256`：授权导入的原始资料，按内容哈希保存。
- `data/raw/weflow/sha256`：WeFlow API 或 ChatLab 文件的原始 JSON 快照。
- `data/index/pkas.sqlite`：元数据、全文索引、运行记录和审计记录。
- `data/distill/exports`：经过批准后导出的蒸馏 JSONL。
- 密码、令牌、私钥、证书和常见密钥文件名不会导入。
- 私人聊天和第三方内容建议标记为 `restricted`。
- 原始资料、数据库、运行记录和导出数据均被 Git 忽略。

当前仓库没有导入真实个人资料。接入微信等聊天时，应使用用户合法取得的可读导出文件，并先检查样本字段与第三方隐私范围。

## 开发验证

```powershell
uv run ruff check src tests
uv run pyright
uv run pytest
cd web
npm run build
```

系统边界和组件关系见 `docs/architecture.md`，实体、状态与隐私字段见 `docs/data-model.md`。
