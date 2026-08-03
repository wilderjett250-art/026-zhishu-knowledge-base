# 数据模型

## 一、设计原则

- 每个实体具有稳定唯一标识。
- 原始内容与派生内容分开保存。
- 每项派生知识保留可追溯来源。
- 时间、隐私、状态和可信度属于必需元数据。
- 同一事实集只有一个正式归属位置，其他位置通过引用关联。
- 删除、合并、提升和驳回操作保留审计记录。

## 二、主要实体

### Source

表示外部或本地资料来源，例如文件、聊天导出、网页、仓库或录音。

核心字段：id、source_type、original_path、content_hash、created_at、ingested_at、privacy、owner_scope、status。

### Document

表示经过解析的逻辑文档。

核心字段：id、source_id、title、mime_type、language、author、event_time、normalized_path、parser_version。

### Chunk

表示可检索的内容片段。

核心字段：id、document_id、sequence、text、token_count、start_locator、end_locator、embedding_version。

### KnowledgeItem

表示经过整理的事实、概念、决策、经验或说明。

核心字段：id、domain、knowledge_type、title、content、confidence、review_status、valid_from、valid_to、supersedes。

### EvidenceLink

连接知识、个人观察和蒸馏样本与原始证据。

核心字段：subject_id、evidence_id、relation_type、locator、weight、created_at。

### PersonaObservation

表示关于用户偏好、表达、行为或学习状态的候选观察。

核心字段：id、observation_type、statement、first_seen、last_seen、evidence_count、confidence、counterexamples、approval_status。

### DistillationExample

表示可用于个人提示、Skill、RAG 或训练的数据样本。

核心字段：id、example_type、input、preferred_output、rejected_output、rationale、source_ids、privacy、quality_score、approval_status、split。

### WorkflowRun

记录工作流输入、步骤、状态、输出、验证和错误。

### AgentRun

记录任务目标、计划、工具调用、上下文来源、确认点、结果和评估。

### ApprovalRecord

记录用户对候选知识、自我观察、蒸馏样本和高影响操作的批准或驳回。

### Connector 与 ConnectorSnapshot

Connector 记录 WeFlow 等本地数据源的类型、接入方式和状态。ConnectorSnapshot 保存用户确认导入的原始 XLSX 或 ChatLab JSON 哈希、私有仓路径、来源 URI 和采集时间；不保存密钥内容。

### SyncRoot、SyncItem 与 CodexSessionCursor

SyncRoot 记录持续资料源的绝对路径、连接器类型、领域、隐私级别和 `catalog/index` 模式。SyncItem 保存文件目录项、大小、修改时间、指纹、当前状态以及对应 Source。CodexSessionCursor 按原始会话文件记录已读取字节位置和未完成任务的最小状态，使后续同步只读取新增内容。

### Customer 与 CustomerConversation

Customer 表示用户明确选择进入业务系统的微信客户私聊或客户群。CustomerConversation 保存微信会话 ID、会话类型、消息范围、隐私级别、增量游标和最近同步时间。

### CustomerMessage

表示从 WeFlow XLSX 或 ChatLab 标准化后的单条微信消息。核心字段包括可用的平台消息 ID、XLSX 行定位、发送者、是否本人发送、时间、消息类型、正文、回复目标、媒体定位、来源快照、去重哈希和隐私级别。

### CustomerSignal

表示从客户聊天中识别出的需求、承诺、待办、风险、决策、跟进或偏好。智能体只能创建 candidate，必须保留证据消息 ID，由用户审核后才能成为正式客户事实。

## 三、通用状态

内容状态：

- raw：原始导入。
- normalized：完成标准化。
- candidate：候选内容。
- verified：完成证据核对。
- approved：经用户确认。
- superseded：已被新版本替代。
- rejected：已驳回并保留原因。

隐私级别：

- public：允许公开。
- private：仅个人知识库使用。
- restricted：仅在明确授权范围中使用。
- secret-reference：只保存秘密所在位置和用途，不保存秘密值。

领域：

- work：业务与工作。
- self：个人与自我理解。
- shared：跨领域知识。
- distill：蒸馏和评估。

## 四、个性与性格建模规则

- 单次对话只产生候选观察。
- 稳定特征需要跨时间、跨场景的多项证据。
- 记录反例和变化，不把性格描述为永久不变的事实。
- 区分用户原话、外部事实、AI 推断和用户确认。
- 涉及心理或健康的内容只记录可观察行为和用户自述，不自动生成诊断结论。
