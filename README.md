# 知枢：个人知识智能体系统

知枢（Personal Knowledge & Agent System，PKAS）是一个本地优先的个人智能系统。它把业务资料、工作经验、聊天导出、学习材料和个人偏好放进同一个逻辑知识库，让 Codex 在每个新任务中自行检索所需证据，同时保留工作流、智能体运行、自我画像和未来蒸馏数据。

它不是一个“把所有文件塞给模型”的大文件夹。资料原件、可检索索引、长期画像和训练候选互相分离，但可以沿来源关系追溯。

## 已有能力

- 明确路径的只读检查、敏感文件跳过、SHA-256 原件留存和内容去重。
- TXT、Markdown、代码、JSON/JSONL 聊天、CSV、HTML、PDF、DOCX、XLSX、PPTX、EML 结构化解析。
- PDF 页面/文字块/坐标、Office 标题/表格/公式/批注/备注及解析质量报告。
- 正式 `Source → Document → Block → Child Chunk` 数据模型；普通文档、表格、代码和消息采用不同的 typed-v1 切片配置。
- 可插拔 Docling 格式补全与 PaddleOCR-VL-1.6 扫描页视觉解析；只处理原生解析不足的内容。
- SQLite FTS5 中文全文检索，以及 Qdrant + 云端 Embedding 的可降级混合检索。
- 持期资料源目录：可先盘点现成文件，再按目录选择 catalog 或 index 增量同步。
- Codex 原始历史保持不变；新任务默认不入库，只有用户明确授权后才提取最小工作事实。
- 检索结果到原文件、仓内原件、文档和段落定位的证据链。
- 可记录步骤、状态、输出和失败信息的资料导入与索引工作流。
- SQLite 同事务写入的索引 Outbox：Chunk/Source 变化可在中断后恢复，向量覆盖达到 100% 才确认事件完成；Codex 用户请求不产生向量事件。
- Codex MCP 工具：检索、读原文、列来源、检查/确认导入、准备智能体上下文、提交画像与蒸馏候选。
- WeFlow 导出记录发现、XLSX 会话选择、ChatLab `0.0.2` 兼容导入和消息去重。
- 微信客户档案、聊天时间线、客户消息检索、需求/承诺/待办审核和回复上下文。
- 自我画像人工批准机制，以及已批准蒸馏样本的标准 JSONL 导出。
- 一个本地管理台，统一操作知识、来源、工作流、智能体、自我画像、蒸馏和审计记录。
- RAG 实验室：真实向量覆盖、知识细胞投影、150 题来源绑定池、0–3 级逐来源人工标注，以及 Recall/MRR/nDCG/标注覆盖率；未召回但原本绑定的来源会补入复核范围。只有范围内每个来源都由人工确认的 `review_eligible` 题进入 `graded-scope-v2` 正式指标。
- 知识细胞图遍历真实Qdrant集合后，按领域×来源类型平衡抽取300点，细胞大小按片段长度缩放；全库覆盖矩阵同时区分文档FTS、文档向量、微信客户消息FTS、资料目录和已批准长期知识，并解释Codex任务、restricted策略及待向量化数量。
- 管理台启动后可直接访问 `http://127.0.0.1:8765/?page=rag`；银标回归使用 `python scripts/run_rag_silver_gate.py`，低于配置阈值会返回非零退出码。每题保持正式 Top5 口径，同时运行本地不重排 Top20 诊断支路，区分候选未找到、排位不足、重排救回、重排误伤和来源不可用；逐题报告可从管理台下载。
- RAG 实验室“继续下一道待复核题”会自动载入真实 Top10、旧范围来源和未召回的绑定来源；保存后自动进入下一题。`expected-source` 自动绑定只作复核提示，不能替代人工 0–3 级判断。
- 含糊、重复、不可回答、来源错误或超出范围的问题可以人工剔除并记录原因代码；剔除题保留审计但不进入指标，同时增加待补题数量，不能靠坏题凑够 150 条。
- `scripts/install_dashboard.ps1` 可在用户单独明确授权后，将管理台注册为登录后 2 分钟隐藏启动的 `PKAS-Dashboard`，严格绑定 `127.0.0.1:8765`；策略缺失或未显式启用时注册会被拒绝。`scripts/uninstall_dashboard.ps1` 可精确停用并移除任务。
- 总览把原始证据层和默认知识层分开：Codex 用户任务保留用于任务事实提取与审计，但不计入“知识来源/知识片段”，也不出现在最近知识列表。
- DeepSeek V4 知识 Agent：规划检索、读取本地证据、检查授权 Git 状态并整理开发进度。
- LangGraph 状态图与 SQLite 检查点：节点级持久化、中断恢复和运行历史。
- LangChain Core 业务工具：统一封装知识检索、资料目录、项目状态和来源读取。
- 用户明确授权的任务可在交付后排队收尾；用户请求和助手声明都不能充当完成证据。
- DeepSeek Token 日预算、应用缓存、官方上下文缓存统计、费用估算和窄重试机制。
- 每日可验证恢复包：SQLite 在线一致性快照、LangGraph 检查点、全部哈希原文、逐文件 SHA-256、三版保留和隔离目录恢复演练；密钥、运行日志与可重建 Qdrant 向量不进入恢复包。

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

## 复制到另一台 Windows 电脑

`scripts/export_windows_package.ps1` 可生成不含 `data/`、聊天、数据库、运行日志、`.env`、DPAPI 密钥、虚拟环境和 Qdrant 数据的软件包，并为每个源码文件生成 SHA-256 清单。目标电脑使用 `scripts/preflight_windows.ps1` 和 `scripts/bootstrap_windows.ps1` 完成依赖、Web、Qdrant、空库及 FTS 隔离验收；只有显式传入 `-RegisterTasks` 才注册开机任务。

完整命令、验收报告和卸载边界见 [Windows 复制安装与恢复](docs/windows-replication.md)。只有在目标电脑用户明确启用本地自启动策略并请求注册任务后，才会创建包括每日 03:00 `PKAS-Daily-Backup` 在内的任务；安装器不会启用 Codex MCP，也不会复制任何 API 密钥。

## 备份与灾难恢复

正式恢复包保存在 `data/backups/scheduled/pkas-scheduled-*`。每个包包含主 SQLite、存在时的 LangGraph 检查点、全部 `data/raw` 哈希原文和 `manifest.json`；明确排除 `.env`、DPAPI/API 密钥、Qdrant、运行日志及已有备份。Qdrant 是派生索引，恢复后必须从 SQLite + 原文重建。

手动创建、验证或旁路恢复：

```powershell
E:\codex-kb\.venv\Scripts\python.exe -m pkas.backup_worker --label scheduled --retention 3
E:\codex-kb\.venv\Scripts\python.exe scripts\verify_restore.py E:\恢复包目录
E:\codex-kb\.venv\Scripts\python.exe scripts\verify_restore.py E:\恢复包目录 --target-data-root E:\PKAS-restored-data
```

恢复永远拒绝覆盖当前活动数据根或非空目录。恢复副本会重映射原文路径、禁用所有同步根，并核对 SQLite `quick_check`、原文哈希以及 `chunks == chunks_fts`；确认后再单独配置目标电脑密钥、重建 Qdrant 并人工启用同步根。

## 混合 RAG 与向量库

SQLite、原始资料库和来源定位仍是正式事实源；Qdrant 只保存可重建的语义向量。检索会融合 FTS5 精确召回与 Qdrant 语义召回。Embedding 未配置或 Qdrant 不可用时，系统继续使用 FTS5；已经启用语义能力但索引不完整或服务异常时，API/MCP 会返回降级警告。

Qdrant Payload 不保存正文、标题或本机原始路径；语义命中后按 `chunk_id` 从 SQLite 回读。生成 Embedding 时会加入最多五层的有界路径标签，避免大量 `index.vue`、`README.md` 同名源码互相混淆；路径标签只参与允许远程处理资料的向量生成，不进入 Qdrant Payload。混合候选经过来源多样化，复杂查询才按需调用 SiliconFlow Rerank，最后返回结构化充分性状态和正式 Block 父级上下文。`restricted` 候选默认不发送云端重排。

本机 Qdrant 1.19.0 安装在 `runtime/qdrant`（运行时二进制不进 Git），数据保存在 `data/qdrant`。启动并验证：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\codex-kb\scripts\start_qdrant.ps1
Invoke-RestMethod http://127.0.0.1:6333/
```

SiliconFlow 密钥不写入 `.env`、源码、数据库或知识库。由当前 Windows 用户在隐藏输入框中粘贴后，使用 DPAPI 加密保存：

```powershell
E:\codex-kb\.venv\Scripts\pkas.exe configure-embedding
```

首次或资料发生变化后执行增量向量化：

```powershell
E:\codex-kb\.venv\Scripts\python.exe -m pkas.vector_worker --max-chunks 200
```

默认模型为 `BAAI/bge-m3`。每次默认最多处理 200 个变化片段，`--max-chunks 0` 才表示不设上限；`codex-turn` 不进入语义索引，`restricted` 资料默认不发送到云端。只有明确设置 `PKAS_EMBEDDING_ALLOW_RESTRICTED_REMOTE_PROCESSING=true` 才会允许后者。

正式后台入口为单实例 Core Worker。它通过 Windows 文件锁避免两个进程同时刷新 Qdrant，并消费可恢复 Outbox 与 Agent 队列：

```powershell
E:\codex-kb\.venv\Scripts\python.exe -m pkas.core_worker --interval-seconds 300
```

`scripts/install_core_worker.ps1` 可注册登录后隐藏启动的当前用户任务；它只处理 Outbox 和已排队 Agent，可与负责六小时增量采集的 `PKAS-Knowledge-Sync` 并存。回滚入口为 `scripts/uninstall_core_worker.ps1`。上线前必须确认 Agent pending 数量和预算，防止历史积压触发云端调用。

首次导入资料时，在“资料接入”中填写一个准确的绝对路径，先执行只读检查，再选择领域和隐私级别并确认导入。系统不会自行扫描整块磁盘，也不会自动导入任何个人资料。

## 现成数据与持续同步

“一个知识库”是统一的检索和权限入口，不是把所有原始文件复制成一个巨型数据库。系统提供两种持续资料源模式：

- `catalog`：保存文件路径、大小、修改时间和状态，让 Codex 先知道资料在哪里；不复制正文。
- `index`：对受支持且非敏感的文件抽取正文、保存哈希原件并建立检索索引；再次扫描只处理变化。

Codex 会话使用专用 `codex_sessions` 连接器。首次同步会流式读取历史 JSONL，派生层只保留每轮用户请求、任务 ID、时间和工作目录；助手最终回答、推理、工具调用输出、图片/音频二进制不进入知识库。之后由 Codex `agent-turn-complete` 通知写入新的用户任务，同一 turn 重复通知不会重复入库，常见密钥会在落盘前脱敏。

已注册资料源可以通过 `uv run --no-sync pkas-sync-worker` 一次完成增量刷新。正式环境使用 Codex 本地自动化每日调用该入口，自动化只处理已经完成一次性授权的范围。

管理台“知识与来源”会显示每个持续资料源的有效项、已索引项、安全跳过项、不可读取项和最近同步时间；可以逐源执行增量刷新，也可以按文件名或路径片段检索资料地图。历史版本以及 Codex 桌面内部的 ambient-suggestion、自动标题和活动状态生成任务不会进入可检索结果。全文检索只补充已审核通过的知识项；未审核候选保留用于追溯，但不会回流到 Agent 上下文。

## 混合文档解析

文档解析采用三条互补通道：代码、JSON、Office 单元格等内容先由原生解析器精确读取；复杂格式可由 Docling 补充为统一 Markdown；扫描 PDF 和图片只在原生文字不足时调用完整的 PaddleOCR-VL-1.6 服务。解析结果按页面、标题、表格、幻灯片和坐标形成语义块，再进入 FTS5，不再只按固定字符数盲切。

普通安装不下载或启动本地文档模型。Docling 是独立可选依赖：

```powershell
uv sync --extra document-ai
```

PaddleOCR 使用官方 PaddleX `POST /layout-parsing` 服务契约。服务可以部署在独立 GPU 主机；本机只发送被质量规则判定为缺少文字的页面。启用配置为：

```powershell
PKAS_DOCUMENT_PADDLEOCR_ENABLED=true
PKAS_DOCUMENT_PADDLEOCR_BASE_URL=http://127.0.0.1:8080
PKAS_DOCUMENT_ALLOW_REMOTE_PROCESSING=true
```

远程处理默认关闭，`restricted` 资料即使启用普通远程处理也不会发送。只有单独设置 `PKAS_DOCUMENT_ALLOW_RESTRICTED_REMOTE_PROCESSING=true` 才会改变这条边界。

人工标注评测清单采用 UTF-8 JSONL，每行包含 `path`，并可包含 `expected_text`、`expected_fields`、`expected_block_types`。运行：

```powershell
uv run pkas-extraction-eval E:\评测资料\manifest.jsonl
```

报告同时给出旧版与混合解析的文字召回率、准确率、F1、关键字段召回率和结构类型召回率；报告只记录序号与聚合指标，不复制标注正文和文件名。

## WeFlow 微信会话与客户闭环

先由 WeFlow 使用它已保存的数据库连接打开微信记录并导出 XLSX。知枢不读取 `decryptKey`、不直接打开 WCDB，也不依赖 WeFlow HTTP API；它只读取 WeFlow 自己维护的 `weflow-export-records.json`，再处理用户明确选择的现存导出文件。

当前电脑采用“手动一键增量同步”，不再注册定时任务或开机启动。管理台会先读取知识库中全部已导入微信会话的同步水位，以最晚消息时间减一天作为容错起点，只为这些既有会话创建一次性 WeFlow 导出任务；WeFlow 通过无控制台子进程运行，导出结束后仅导入本次新生成的记录并按消息唯一键去重，随后回收进程和临时任务。若用户已经打开 WeFlow，系统不会强制关闭，而是提示退出后重试。新增联系人仍通过现有人工选择入口首次导入，避免静默扩大采集范围。

PKAS 后台同步会额外传入 WeFlow 专用后台标志，使其从启动源头不创建 Splash、可见主窗口、托盘和通知；这不是“显示后再关闭”。用户自行正常启动 WeFlow 时不带该标志，WeFlow 的正常主界面仍会出现。

管理台操作入口为“WeFlow / 资料”：

1. 点击“发现现存 XLSX 导出”。默认自动定位 `%APPDATA%\weflow\weflow-export-records.json`，也可以填写准确的绝对路径。
2. 系统只读列出仍然存在的导出文件；勾选需要进入本地知识库的私聊或群聊。
3. 先检查 XLSX 表头、文件哈希、会话数量和声明消息数，并根据 XLSX 元数据跳过重复会话别名，再明确确认导入。
4. 原始 XLSX 保存为 SHA-256 快照，消息按会话、发送者、时间、类型与内容去重，统一以 `restricted + candidate` 建立待归类微信会话索引。
5. 在“微信会话”中人工核对身份；只有保存为 `approved` 的会话才进入客户回复与业务信号工作流。
6. 对已确认客户查看时间线、检索历史沟通、审核需求/承诺/待办，并为 Codex 准备回复依据。

ChatLab JSON 仍作为兼容入口保留。两种方式都不会直接操作微信或自动发送客户消息。

## 接入 Codex

MCP 服务启动命令：

```powershell
E:\codex-kb\.venv\Scripts\python.exe -m pkas.mcp_server
```

Codex 在 Windows 上应直接调用已经同步完成的虚拟环境。不要在 MCP 配置中使用
`uv run pkas-mcp`，否则多个 Codex 任务并发启动时，`uv` 可能尝试更新正在使用的
入口程序并导致 stdio 连接关闭。

把该 stdio 命令注册为 Codex 的 MCP 服务后，Codex 可调用以下工具：

- `search_knowledge`：检索带来源的知识片段；Codex 任务只包含用户请求，助手回答不会进入知识库。旧参数 `include_unverified_claims` 仅保留客户端兼容性。
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
- `list_weflow_customers`：默认只列出已确认客户；人工整理时可显式包含待归类会话。
- `get_customer_timeline`：读取授权客户的聊天时间线。
- `search_customer_messages`：检索需求、报价、进度和历史承诺。
- `prepare_customer_reply_context`：准备客户档案、聊天证据和工作知识。
- `save_customer_signal_candidate`：提交待审核的需求、承诺、待办或风险。

默认规则是：先检索再回答；restricted 资料不自动返回；导入必须先检查并明确确认；自我画像和蒸馏样本只能由智能体创建候选，最终批准权属于用户。

## LangGraph + DeepSeek Agent

系统不部署本地生成模型。确定性的扫描、解析、去重、FTS5 检索、Git 状态读取和结果校验全部在本地执行；只有查询规划、跨证据综合和任务收尾调用 DeepSeek API。

Agent 编排使用 LangGraph，业务工具使用 LangChain Core 的 `StructuredTool`。知识任务固定为“项目观察 → 初始检索 → 检索规划 → 工具执行 → 证据综合 → 候选写回”六个节点；Codex 收尾固定为“项目观察 → 状态综合 → 候选写回”三个节点。每个节点完成后写入独立 SQLite 检查点，失败时保留待执行节点，恢复操作不会重复已经完成的步骤。

PKAS 在每次图执行和检查点读取期间显式关闭 LangSmith tracing，即使电脑全局环境曾启用追踪，也不会把知识片段、Agent 状态或提示词发送给 LangSmith。

在本机 `.env` 中配置：

```powershell
PKAS_DEEPSEEK_API_KEY=
PKAS_DEEPSEEK_FLASH_MODEL=deepseek-v4-flash
PKAS_DEEPSEEK_PRO_MODEL=deepseek-v4-pro
PKAS_AGENT_DAILY_INPUT_TOKEN_BUDGET=200000
PKAS_AGENT_DAILY_OUTPUT_TOKEN_BUDGET=20000
PKAS_AGENT_DAILY_CLOSEOUT_ENABLED=true
PKAS_AGENT_DAILY_TIMEZONE=local
PKAS_AGENT_DAILY_MAX_JOBS=1
PKAS_AGENT_DAILY_MAX_SOURCES_PER_THREAD=3
PKAS_AGENT_DAILY_MAX_SOURCES_TOTAL=30
```

普通任务固定使用 V4 Flash 非思考模式；只有显式指定复杂任务时才使用 V4 Pro 思考模式。相同模型、提示词版本和输入会命中本地结果缓存，不重复调用 API。临时网络、限流或服务端故障最多重试一次；鉴权、参数和预算错误直接停止。

Agent Prompt 已采用版本化工程管理。检索规划、证据综合使用 v2 Prompt，Codex 每日收尾使用 `codex-closeout-v4-user-task-only`，明确规定用户任务只代表“要求做什么”、结论必须绑定独立证据、源码/测试/部署/目标环境验收必须分层。JSON 在进入状态图前经过业务字段校验；结构正确但必填字段缺失时只执行一次定向修复，并把首次调用的 Token 计入预算。三类任务的输出上限分别为 420、1000 和 700 Token，最小可靠额度分别为 200、700 和 300；接近日预算时会自动压缩到剩余额度，低于对应最小额度才暂停。

新 Codex 回合完成后，通知入口只提取并脱敏用户请求，再去重和建立全文索引；助手最终回答不会写入派生文件、SQLite 文档或 FTS。该入口不调用 DeepSeek，也不创建逐回合后台任务。每日增量同步完成后，系统读取上次截止点以来的新用户任务，按线程聚合；只有出现实现、修改、测试、构建、部署或 Git 操作等任务意图的线程才创建一个 `codex_daily_closeout`。同一线程的重复任务会合并，内部标题生成和普通问答不会消耗 Agent Token。

每日收尾由现有 `pkas.sync_worker` 统一触发。预算不足时任务保持待执行且不消耗重试次数，下一次日同步继续处理。原始 Codex JSONL 会话文件保持不变，但知识库派生层只保留用户请求；旧版派生记录会自动剥离助手回答，无法可靠分离的记录直接退出索引。只有用户任务而没有独立机器证据时，Agent 结果留在运行日志中，不创建知识候选。

手动处理排队任务：

```powershell
E:\codex-kb\.venv\Scripts\python.exe -m pkas.agent_worker --max-jobs 3
```

Codex MCP 还提供：

- `run_knowledge_agent`：规划查询、执行本地检索并综合当前状态。
- `run_codex_closeout`：对指定 Codex 来源执行任务收尾。
- `get_agent_graph_status`：查看检查点、待执行节点和是否可恢复。
- `resume_agent_run`：从最后一个成功检查点恢复 Agent。
- `list_agent_jobs`：查看等待、完成和失败的后台任务。
- `get_agent_token_usage`：查看今日 Token、缓存命中和估算费用。

## 命令行

```powershell
uv run pkas init
uv run pkas inspect "E:\明确的资料目录"
uv run pkas ingest "E:\明确的资料目录" --domain work --privacy private --yes
uv run pkas reindex-outdated --yes
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
- `data/index/langgraph-checkpoints.sqlite`：LangGraph 节点状态、检查点和恢复历史。
- `data/backups/scheduled`：不含密钥的三版可验证恢复包；Qdrant 恢复后重建。
- `data/distill/exports`：经过批准后导出的蒸馏 JSONL。
- 密码、令牌、私钥、证书和常见密钥文件名不会导入。
- Codex 自动写回只保存用户请求；助手回答不进入派生文档或检索索引，并在写入前执行常见凭证脱敏。
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
