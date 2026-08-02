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
