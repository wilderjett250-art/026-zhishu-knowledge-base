# ADR-002：客户端无关的本地 AI 能力中台

- 状态：accepted
- 日期：2026-08-28
- 决策范围：PKAS 产品边界、模块边界和客户端接入

## 背景

现有 PKAS 已具备知识库、FTS、Qdrant、RAG、Agent、工作流、MCP 和 Web 管理台，但产品叙述和部分代码仍以 Codex 为中心。用户明确要求最终产品能够增强同类型 AI 应用，并统一管理 Skill、MCP、知识、向量、Agent 和工作流，后续可在其他电脑安装和闭源交付。

开源参考证明了若干有效交互模型：mTarsier 的跨客户端发现与配置回滚、Moor 的单一 MCP Gateway 与 Profile、MCP Inspector 的协议实验台、SkillServer 的 Skill 资源与 Git 只读同步。PKAS 只参考产品机制和公开协议，不复制第三方界面或代码。

## 决策

1. PKAS 定位为客户端无关的本地 AI Control Plane，Codex 是首个 Client Adapter。
2. 保持模块化单体，不为个人规模引入微服务；一个 Core Runtime 拥有数据库、索引、任务、预算和审计状态。
3. 对外接入统一经过 Integration Gateway，依次提供 MCP、REST、OpenAI-compatible API、CLI/SDK。
4. 新增 Capability Registry 和 Client Adapter SPI。客户端配置解析、渲染、差异、备份、写入和验证均由适配器实现。
5. Skill、MCP 和知识属于不同资产类型，统一展示但不混用存储和执行权限。
6. 客户端配置写入默认关闭；未来启用时必须满足备份、脱敏、原子写、语法验证、实际连接测试和自动回滚。
7. 商业桌面壳、授权、更新和秘密管理是发行边界，不进入知识/RAG领域逻辑。

## 技术选择

- 当前 Core：Python 3.11、FastAPI、SQLite/FTS5、Qdrant、LangGraph。
- 当前 Web：React、TypeScript、Vite。
- 商业发行目标：Tauri 2 + Rust 宿主负责安装、进程监管、配置事务、Secret Store、授权和签名更新；Python Core 以锁定依赖的独立可执行产物交付。
- 协议：MCP Streamable HTTP/stdio、REST/OpenAPI、OpenAI-compatible API。

## 后果

- 现有知识/RAG代码可继续使用，无需推倒重来。
- Codex专属代码必须逐步迁入 `adapters/codex` 边界，不能继续向领域核心扩散。
- 新增客户端只实现适配器和验收，不复制知识库。
- “闭源”只能提高复制和篡改成本，不能承诺绝对不可逆向；商业保护还需要许可证、签名、更新和服务端权益控制。

## 当前证据

- `src/pkas/capability_registry.py` 已建立 Client Adapter 协议、Codex 只读发现和脱敏能力总览。
- `/api/capabilities/overview` 已建立第一版稳定查询接口。
- `web/src/CapabilityCenter.tsx` 已建立统一能力中心页面。
- 聚焦 pytest、Ruff 和 Web production build 已通过；真实客户端配置写入仍未实现，也未被本 ADR 宣称完成。
