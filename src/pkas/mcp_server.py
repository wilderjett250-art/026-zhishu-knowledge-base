from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from pkas.config import get_settings
from pkas.customer_service import CustomerReviewRequired
from pkas.search_gateway import UnifiedSearchRequest, unified_search
from pkas.system import KnowledgeSystem
from pkas.usage_metrics import observe_tool

mcp = MCPServer(
    "personal-knowledge-agent",
    title="个人知识与智能协作系统",
    description=(
        "为 Codex 提供本地个人知识检索、来源阅读、明确授权导入、"
        "自我画像候选和蒸馏样本候选工具。"
    ),
    instructions=(
        "先检索再回答；重要结论保留 source_id、document_id、locator 和 original_uri。"
        "不要猜测未检索到的个人事实。导入资料必须先调用 inspect_import_path，"
        "只有用户明确批准完全相同的路径、领域和隐私范围后，才调用 import_confirmed_path。"
        "已注册的 sync root 是用户持续授权的资料范围；任务需要最新文件地图时可直接增量扫描，"
        "再调用 search_source_catalog 定位文件。"
        "WeFlow 导出也必须先发现并检查；只有用户明确批准同一批会话后才允许确认导入。"
        "个人画像和蒸馏样本只能创建候选，批准操作由用户在管理台完成。"
        "微信客户聊天默认是 restricted；只有任务明确需要且用户授权时才读取。"
        "回复客户时只生成草稿，不直接发送微信消息。普通资料检索由 Codex 自己编排，"
        "本地同步和检索不消耗生成模型 Token。"
    ),
    version="0.1.2",
)

_SYSTEM: KnowledgeSystem | None = None
_SYSTEM_LOCK = Lock()

READ_ONLY_TOOL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
ADDITIVE_IDEMPOTENT_TOOL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
ADDITIVE_TOOL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
AI_TOOL = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


def agent_mcp_tool(*args, **kwargs):
    """Keep the legacy PKAS Agent implementation out of the active MCP surface.

    Codex remains the interactive agent.  The old LangGraph/DeepSeek tools are
    only re-enabled explicitly for a controlled recovery build and are not
    advertised to normal MCP clients.
    """

    def decorate(function):
        if get_settings().agent_runtime_enabled:
            return mcp.tool(*args, **kwargs)(function)
        return function

    return decorate


def system() -> KnowledgeSystem:
    """Reuse one application graph for the lifetime of this MCP process.

    MCP tool calls are handled by the same stdio server. Recreating the full
    KnowledgeSystem for every call throws away the HTTP connection pools and
    vector client cache, which adds avoidable startup and TLS latency without
    changing retrieval behavior.
    """
    global _SYSTEM
    if _SYSTEM is None:
        with _SYSTEM_LOCK:
            if _SYSTEM is None:
                _SYSTEM = KnowledgeSystem.create()
    return _SYSTEM


def envelope(
    summary: str,
    data: Any,
    *,
    status: str = "success",
    next_actions: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "summary": summary,
        "data": data,
        "next_actions": next_actions or [],
        "artifacts": artifacts or [],
    }


@mcp.tool(
    description=(
        "与EXE共用统一混合检索，返回来源、实际模式、参数和降级提示。"
        "默认只查文件，不混入Codex任务请求；scopes可显式加入chats，聊天先查本地全文，"
        "明确包含受限资料时再查受控的会话摘要向量。"
        "默认不返回 restricted 资料；包含受限资料不改变云端处理授权。"
        "文件按既有配置调用Embedding和重排API。"
        "助手回答在采集层已排除；include_unverified_claims 仅为旧客户端兼容参数。"
        "提供 workspace_path 时只返回该项目及其子目录来源，避免跨项目误召回。"
    ),
    annotations=READ_ONLY_TOOL,
)
@observe_tool
def search_knowledge(
    query: str,
    domain: str | None = None,
    limit: int = 10,
    include_restricted: bool = False,
    include_unverified_claims: bool = False,
    scopes: list[Literal["files", "chats"]] | None = None,
    rerank_mode: Literal["auto", "never", "always"] = "auto",
    expand_parent: bool = True,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    response = unified_search(
        system(),
        UnifiedSearchRequest(
            query=query,
            workspace_path=workspace_path,
            domain=domain,
            limit=max(1, min(limit, 50)),
            include_restricted=include_restricted,
            scopes=scopes if scopes is not None else ["files"],
            rerank_mode=rerank_mode,
            expand_parent=expand_parent,
        ),
    )
    summary = f"找到 {len(response['results'])} 条带来源的知识片段（{response['mode']}）"
    result = envelope(
        summary,
        response["results"],
        status="warning" if response["warnings"] else "success",
        next_actions=response["warnings"],
    )
    result["retrieval"] = response
    return result


@mcp.tool(
    description="按 document_id 阅读知识库中的原文，可分页读取长文档。",
    annotations=READ_ONLY_TOOL,
)
@observe_tool
def read_document(
    document_id: str,
    offset: int = 0,
    limit: int = 12000,
) -> dict[str, Any]:
    item = system().repository.read_document(
        document_id,
        max(0, offset),
        max(1, min(limit, 50000)),
    )
    if not item:
        return envelope(
            "资料不存在",
            None,
            status="error",
            next_actions=["重新检索并使用有效的 document_id"],
        )
    return envelope("已读取原文与来源定位", item, artifacts=[item["vault_path"]])


@mcp.tool(
    description="列出最近导入的本地资料来源及其领域、隐私级别和哈希。",
    annotations=READ_ONLY_TOOL,
)
def list_sources(limit: int = 50) -> dict[str, Any]:
    items = system().repository.list_sources(max(1, min(limit, 200)))
    return envelope(f"已读取 {len(items)} 个资料来源", items)


@mcp.tool(
    description=(
        "只读检查一个用户明确给出的绝对路径，统计可导入、敏感和不支持文件；"
        "不会复制、解析或索引资料。正式导入前必须先调用本工具。"
    ),
    annotations=READ_ONLY_TOOL,
)
def inspect_import_path(path: str, recursive: bool = True) -> dict[str, Any]:
    try:
        result = system().ingestion.inspect_path(path, recursive)
    except (OSError, ValueError) as exc:
        return envelope(
            "导入范围检查失败",
            {"error": str(exc)},
            status="error",
            next_actions=["请用户确认准确的绝对路径和目录边界"],
        )
    return envelope(
        "导入范围检查完成，尚未复制或索引任何文件",
        result,
        next_actions=["向用户展示检查结果，并取得对同一路径、领域和隐私级别的明确批准"],
    )


@mcp.tool(
    description=(
        "将明确授权路径中的资料复制到哈希原件库并建立全文索引。"
        "必须先检查范围，并把 confirmed 设为 true；敏感密钥文件仍会跳过。"
    ),
    annotations=ADDITIVE_IDEMPOTENT_TOOL,
)
def import_confirmed_path(
    path: str,
    inspection_token: str = "",
    domain: str = "work",
    privacy: str = "private",
    recursive: bool = True,
    confirmed: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        return envelope(
            "未执行导入：缺少用户明确确认",
            {"path": path, "domain": domain, "privacy": privacy},
            status="warning",
            next_actions=["先调用 inspect_import_path，并取得用户对完整范围的明确确认"],
        )
    if len(inspection_token) != 64:
        return envelope(
            "未执行导入：缺少有效的检查令牌",
            {"path": path, "domain": domain, "privacy": privacy},
            status="warning",
            next_actions=["重新调用 inspect_import_path，并传入其返回的 inspection_token"],
        )
    if domain not in {"work", "self", "shared", "distill"}:
        return envelope("领域参数无效", {"domain": domain}, status="error")
    if privacy not in {"public", "private", "restricted"}:
        return envelope("隐私参数无效", {"privacy": privacy}, status="error")
    result = system().workflows.run_import(
        path=path,
        recursive=recursive,
        domain=domain,
        privacy=privacy,
        inspection_token=inspection_token,
    )
    status = "success" if result["status"] != "failed" else "error"
    summary = (
        f"导入完成，新增 {result['result']['imported']} 个资料来源"
        if status == "success"
        else "导入工作流未完成"
    )
    return envelope(summary, result, status=status, artifacts=result.get("artifacts", []))


@mcp.tool(
    description=(
        "列出已注册的资料源、同步模式、最近扫描时间和索引数量。"
        "catalog 只建立文件目录；index 会抽取受支持正文。"
    ),
    annotations=READ_ONLY_TOOL,
)
def list_sync_roots() -> dict[str, Any]:
    items = system().sync.list_roots()
    return envelope(f"已读取 {len(items)} 个资料源", items)


@mcp.tool(
    description=(
        "注册一个用户明确指定的本地目录为持续资料源。必须 confirmed=true。"
        "local_files 支持目录盘点或正文索引；codex_sessions 只抽取用户请求与最终回答，"
        "不会收集推理、工具输出或附件二进制。"
    ),
    annotations=ADDITIVE_IDEMPOTENT_TOOL,
)
def register_sync_root(
    name: str,
    root_path: str,
    connector_type: str = "local_files",
    domain: str = "work",
    privacy: str = "private",
    sync_mode: str = "catalog",
    recursive: bool = True,
    confirmed: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        return envelope(
            "未注册资料源：缺少用户明确确认",
            {"name": name, "root_path": root_path, "sync_mode": sync_mode},
            status="warning",
        )
    try:
        item = system().sync.register_root(
            name=name,
            root_path=root_path,
            connector_type=connector_type,
            domain=domain,
            privacy=privacy,
            sync_mode=sync_mode,
            recursive=recursive,
        )
    except (OSError, ValueError) as exc:
        return envelope("资料源注册失败", {"error": str(exc)}, status="error")
    return envelope("资料源已注册，尚未扫描内容", item)


@mcp.tool(
    description=(
        "扫描一个已经由用户确认注册的持续资料源并增量更新，不需要重复确认。"
        "catalog 模式只保存文件路径、大小和修改时间；index 模式还会抽取正文。"
    ),
    annotations=ADDITIVE_IDEMPOTENT_TOOL,
)
def scan_sync_root(root_id: str) -> dict[str, Any]:
    try:
        result = system().sync.scan_root(root_id)
    except (OSError, ValueError) as exc:
        return envelope("资料源同步失败", {"error": str(exc)}, status="error")
    status = "warning" if result.get("errors") else "success"
    return envelope("资料源增量同步完成", result, status=status)


@mcp.tool(
    description="按文件名或相对路径搜索资料目录；catalog 模式下也可定位尚未抽取的现成文件。",
    annotations=READ_ONLY_TOOL,
)
@observe_tool
def search_source_catalog(
    query: str,
    root_id: str | None = None,
    limit: int = 50,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    items = system().sync.search_catalog(
        query,
        root_id=root_id,
        workspace_path=workspace_path,
        limit=limit,
    )
    return envelope(f"在资料目录中找到 {len(items)} 个文件", items)


@mcp.tool(
    description=(
        "只读发现 WeFlow 已完成并仍存在的 XLSX 导出。不会访问数据库密钥、WCDB 或 HTTP API，"
        "也不会导入聊天正文。"
    ),
    annotations=READ_ONLY_TOOL,
)
def discover_weflow_exports(
    records_path: str | None = None,
    keyword: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    try:
        result = system().weflow.discover_exports(
            records_path=records_path,
            keyword=keyword,
            limit=max(1, min(limit, 1000)),
        )
    except (OSError, ValueError) as exc:
        return envelope(
            "WeFlow 导出发现失败",
            {"error": str(exc)},
            status="error",
            next_actions=["确认 WeFlow 导出记录文件和 XLSX 仍然存在"],
        )
    return envelope(
        f"发现 {result['existing_sessions']} 个有现存 XLSX 的 WeFlow 会话",
        result,
        next_actions=["选择明确会话，或在用户批准全部现有会话后执行只读检查"],
    )


def _resolve_weflow_selection(
    knowledge_system: KnowledgeSystem,
    *,
    records_path: str | None,
    session_ids: list[str] | None,
    all_existing: bool,
) -> tuple[str | None, list[str]]:
    if all_existing:
        catalog = knowledge_system.weflow.discover_exports(
            records_path=records_path,
            limit=1000,
        )
        return catalog["records_path"], [item["session_id"] for item in catalog["items"]]
    return records_path, list(dict.fromkeys(session_ids or []))


@mcp.tool(
    description=(
        "只读检查明确选择的 WeFlow XLSX，计算哈希、消息数和检查令牌，不导入正文。"
        "all_existing=true 只适用于用户明确要求全部现有导出时。"
    ),
    annotations=READ_ONLY_TOOL,
)
def inspect_weflow_exports(
    session_ids: list[str] | None = None,
    records_path: str | None = None,
    all_existing: bool = False,
) -> dict[str, Any]:
    knowledge_system = system()
    try:
        resolved_path, resolved_ids = _resolve_weflow_selection(
            knowledge_system,
            records_path=records_path,
            session_ids=session_ids,
            all_existing=all_existing,
        )
        if not resolved_ids:
            return envelope(
                "没有可检查的 WeFlow 会话",
                {"records_path": resolved_path, "session_ids": []},
                status="warning",
            )
        result = knowledge_system.weflow.inspect_export_selection(
            records_path=resolved_path,
            session_ids=resolved_ids,
        )
    except (OSError, ValueError) as exc:
        return envelope(
            "WeFlow 导出检查失败",
            {"error": str(exc)},
            status="error",
            next_actions=["重新发现导出并确认相同的会话范围"],
        )
    result["session_ids"] = resolved_ids
    return envelope(
        f"已检查 {result['selected_sessions']} 个 WeFlow 会话，尚未导入聊天",
        result,
        next_actions=["向用户展示总会话、消息和字节数，并取得对完全相同范围的明确批准"],
    )


@mcp.tool(
    description=(
        "导入已经检查且由用户明确批准的 WeFlow XLSX，会保存哈希原始快照并建立 restricted 客户索引。"
        "必须传入检查令牌并设置 confirmed=true；部分失败可安全重试。"
    ),
    annotations=ADDITIVE_IDEMPOTENT_TOOL,
)
def import_confirmed_weflow_exports(
    inspection_token: str,
    session_ids: list[str] | None = None,
    records_path: str | None = None,
    all_existing: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        return envelope(
            "未执行 WeFlow 导入：缺少用户明确确认",
            {"records_path": records_path, "session_ids": session_ids or []},
            status="warning",
            next_actions=["先调用 inspect_weflow_exports，并取得用户对完全相同会话范围的明确批准"],
        )
    if len(inspection_token) != 64:
        return envelope(
            "未执行 WeFlow 导入：检查令牌无效",
            None,
            status="warning",
            next_actions=["重新调用 inspect_weflow_exports"],
        )
    knowledge_system = system()
    try:
        resolved_path, resolved_ids = _resolve_weflow_selection(
            knowledge_system,
            records_path=records_path,
            session_ids=session_ids,
            all_existing=all_existing,
        )
        result = knowledge_system.customer_workflows.import_weflow_exports(
            records_path=resolved_path,
            session_ids=resolved_ids,
            inspection_token=inspection_token,
            privacy="restricted",
        )
    except (OSError, ValueError) as exc:
        return envelope(
            "WeFlow 导入未执行",
            {"error": str(exc)},
            status="error",
            next_actions=["重新发现并检查同一批导出"],
        )
    status = {"completed": "success", "warning": "warning", "failed": "error"}.get(
        result["status"],
        "error",
    )
    imported = result.get("result", {}).get("imported", 0)
    return envelope(
        f"WeFlow 批量导入完成，新增 {imported} 条客户消息",
        result,
        status=status,
        artifacts=result.get("artifacts", []),
        next_actions=(
            ["查看失败会话后重试；成功消息会自动去重"]
            if result["status"] == "warning"
            else []
        ),
    )


@agent_mcp_tool(
    description=(
        "为当前任务自动选择工作/自我知识范围并准备带证据的上下文。"
        "本工具负责检索与记录，最终推理和行动由调用它的 Codex 智能体完成。"
    ),
    annotations=ADDITIVE_TOOL,
)
def prepare_agent_context(
    task: str,
    domain: str | None = None,
    limit: int = 8,
    include_restricted: bool = False,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    result = system().agent.prepare_context(
        task=task,
        workspace_path=workspace_path,
        domain=domain,
        limit=max(1, min(limit, 30)),
        include_restricted=include_restricted,
    )
    return envelope(result["summary"], result)


@agent_mcp_tool(
    description=(
        "运行 DeepSeek 知识 Agent：先规划查询，再执行本地知识库和资料目录检索，"
        "最后依据证据整理当前状态与下一步。默认不读取 restricted 资料，"
        "默认不把结果写成长期知识。"
    ),
    annotations=AI_TOOL,
)
@observe_tool
def run_knowledge_agent(
    task: str,
    workspace_path: str | None = None,
    domain: str | None = None,
    include_restricted: bool = False,
    persist_result: bool = False,
    complexity: str = "simple",
) -> dict[str, Any]:
    if complexity not in {"simple", "complex"}:
        return envelope("Agent 复杂度参数无效", {"complexity": complexity}, status="error")
    result = system().agent.run(
        task=task,
        workspace_path=workspace_path,
        domain=domain,
        include_restricted=include_restricted,
        persist_result=persist_result,
        complexity=complexity,
    )
    if result["status"] != "completed":
        return envelope(
            "DeepSeek 知识 Agent 未完成任务",
            result,
            status="warning",
            next_actions=[result["error"]["safe_retry"]],
        )
    return envelope(result["result"]["summary"], result)


@agent_mcp_tool(
    description=(
        "对已经写入知识库的 Codex 任务执行开发状态收尾：读取任务证据、"
        "只读检查授权工作区、调用 DeepSeek 整理声明和遗留项，并保存不可直接检索的未审核候选。"
    ),
    annotations=AI_TOOL,
)
@observe_tool
def run_codex_closeout(
    source_id: str,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    result = system().agent.closeout_codex_turn(
        source_id=source_id,
        workspace_path=workspace_path,
    )
    if result["status"] != "completed":
        return envelope(
            "Codex 任务收尾 Agent 未完成",
            result,
            status="warning",
            next_actions=[result["error"]["safe_retry"]],
        )
    return envelope("已生成带证据状态的未审核知识候选", result)


@agent_mcp_tool(
    description="查看 LangGraph Agent 运行的当前检查点、待执行节点和是否可恢复。",
    annotations=READ_ONLY_TOOL,
)
def get_agent_graph_status(run_id: str) -> dict[str, Any]:
    try:
        result = system().agent.graph_status(run_id)
    except ValueError as exc:
        return envelope(str(exc), {"run_id": run_id}, status="error")
    return envelope("已读取 LangGraph Agent 检查点状态", result)


@agent_mcp_tool(
    description=(
        "从最后一个成功检查点恢复失败或中断的 LangGraph Agent 运行。"
        "只继续待执行节点，不重复已经完成的工具步骤。"
    ),
    annotations=AI_TOOL,
)
def resume_agent_run(run_id: str) -> dict[str, Any]:
    result = system().agent.resume(run_id)
    if result["status"] != "completed":
        return envelope(
            "LangGraph Agent 尚未恢复完成",
            result,
            status="warning",
            next_actions=[result["error"]["safe_retry"]],
        )
    return envelope("LangGraph Agent 已从检查点恢复并完成", result)


@agent_mcp_tool(
    description="查看 Codex 任务结束后等待 DeepSeek 整理的本地 Agent 后台任务。",
    annotations=READ_ONLY_TOOL,
)
def list_agent_jobs(limit: int = 100) -> dict[str, Any]:
    items = system().repository.list_agent_jobs(max(1, min(limit, 500)))
    return envelope(f"已读取 {len(items)} 个 Agent 后台任务", items)


@agent_mcp_tool(
    description="查看今日 DeepSeek Agent 的缓存命中、输入、输出和估算费用，不返回密钥。",
    annotations=READ_ONLY_TOOL,
)
def get_agent_token_usage() -> dict[str, Any]:
    knowledge_system = system()
    usage = knowledge_system.repository.llm_usage_since(
        datetime.now(UTC).date().isoformat()
    )
    usage["deepseek_configured"] = knowledge_system.settings.deepseek_enabled
    usage["input_token_budget"] = knowledge_system.settings.agent_daily_input_token_budget
    usage["output_token_budget"] = knowledge_system.settings.agent_daily_output_token_budget
    return envelope("已读取今日 DeepSeek Agent Token 使用量", usage)


@mcp.tool(
    description=(
        "把有证据支持的性格、偏好或习惯保存为待审核候选。不会自动写入已批准的长期个人画像。"
    ),
    annotations=ADDITIVE_TOOL,
)
def save_persona_candidate(
    observation_type: str,
    statement: str,
    evidence_ids: list[str] | None = None,
    confidence: str = "low",
) -> dict[str, Any]:
    if confidence not in {"low", "medium", "high"}:
        return envelope("置信度参数无效", {"confidence": confidence}, status="error")
    item = system().repository.create_persona_candidate(
        observation_type,
        statement,
        evidence_ids or [],
        confidence,
    )
    return envelope(
        "个人观察已保存为候选，等待用户审核",
        item,
        next_actions=["请用户在管理台检查证据和反例后批准或驳回"],
    )


@mcp.tool(
    description=("把一次高质量问答、决策或表达偏好保存为待审核蒸馏样本。不会自动批准或用于训练。"),
    annotations=ADDITIVE_TOOL,
)
def save_distillation_candidate(
    example_type: str,
    input_text: str,
    preferred_output: str,
    rationale: str = "",
    source_ids: list[str] | None = None,
    privacy: str = "restricted",
    rejected_output: str | None = None,
) -> dict[str, Any]:
    if example_type not in {"instruction", "preference", "decision", "conversation"}:
        return envelope("样本类型无效", {"example_type": example_type}, status="error")
    if privacy not in {"public", "private", "restricted"}:
        return envelope("隐私参数无效", {"privacy": privacy}, status="error")
    item = system().repository.create_distillation_candidate(
        example_type=example_type,
        input_text=input_text,
        preferred_output=preferred_output,
        rejected_output=rejected_output,
        rationale=rationale,
        source_ids=source_ids or [],
        privacy=privacy,
    )
    return envelope(
        "蒸馏样本已保存为候选，等待用户审核",
        item,
        next_actions=["审核质量、来源和隐私范围后再批准导出"],
    )


@mcp.tool(
    description=(
        "列出已经由用户确认的微信客户会话。默认排除刚导入但尚未归类的普通微信会话；"
        "只有用户明确要求人工整理待归类会话时才设置 include_candidates=true。"
    ),
    annotations=READ_ONLY_TOOL,
)
def list_weflow_customers(
    limit: int = 100,
    include_candidates: bool = False,
) -> dict[str, Any]:
    items = system().customers.list_customers(
        max(1, min(limit, 500)),
        review_status=None if include_candidates else "approved",
    )
    scope = "微信会话" if include_candidates else "已确认微信客户"
    return envelope(f"已读取 {len(items)} 个{scope}", items)


@mcp.tool(
    description=(
        "读取指定客户的微信时间线。微信聊天默认 restricted，"
        "必须在用户授权当前客户任务后显式设置 include_restricted=true。"
    ),
    annotations=READ_ONLY_TOOL,
)
def get_customer_timeline(
    customer_id: str,
    limit: int = 100,
    before: int | None = None,
    include_restricted: bool = False,
) -> dict[str, Any]:
    customer = system().customers.get_customer(customer_id)
    if not customer:
        return envelope("客户不存在", None, status="error")
    items = system().customers.timeline(
        customer_id,
        limit=max(1, min(limit, 500)),
        before=before,
        include_restricted=include_restricted,
    )
    return envelope(
        f"已读取 {customer['display_name']} 的 {len(items)} 条时间线消息",
        {"customer": customer, "messages": items},
        artifacts=list({item["vault_path"] for item in items if item.get("vault_path")}),
    )


@mcp.tool(
    description=(
        "在 WeFlow 聊天中检索需求、承诺、报价、进度或历史沟通。"
        "未指定 customer_id 时默认只检索已确认客户，并且默认不检索 restricted 聊天。"
    ),
    annotations=READ_ONLY_TOOL,
)
@observe_tool
def search_customer_messages(
    query: str,
    customer_id: str | None = None,
    limit: int = 20,
    include_restricted: bool = False,
    include_candidates: bool = False,
) -> dict[str, Any]:
    items = system().customers.search_messages(
        query,
        customer_id=customer_id,
        limit=max(1, min(limit, 100)),
        include_restricted=include_restricted,
        include_candidates=include_candidates,
    )
    return envelope(
        f"找到 {len(items)} 条微信聊天证据",
        items,
        artifacts=list({item["vault_path"] for item in items if item.get("vault_path")}),
    )


@mcp.tool(
    description=(
        "为指定微信客户准备回复上下文：客户档案、近期聊天、历史匹配、"
        "已批准需求/承诺/待办和相关工作知识。只生成上下文和回复草稿依据，不发送消息。"
    ),
    annotations=ADDITIVE_TOOL,
)
def prepare_customer_reply_context(
    customer_id: str,
    task: str,
    recent_limit: int = 40,
    search_limit: int = 20,
    include_restricted: bool = False,
) -> dict[str, Any]:
    try:
        result = system().customer_service.prepare_reply_context(
            customer_id=customer_id,
            task=task,
            recent_limit=max(1, min(recent_limit, 200)),
            search_limit=max(1, min(search_limit, 100)),
            include_restricted=include_restricted,
        )
    except CustomerReviewRequired as exc:
        return envelope(
            "微信会话尚未确认为业务客户",
            {"customer_id": customer_id, "reason": str(exc)},
            status="warning",
            next_actions=["请用户在本机管理台核对身份并保存为已确认客户"],
        )
    if not result:
        return envelope("客户不存在", None, status="error")
    artifacts = {
        item["vault_path"]
        for group in (result["recent_messages"], result["matched_messages"])
        for item in group
        if item.get("vault_path")
    }
    return envelope(result["summary"], result, artifacts=sorted(artifacts))


@mcp.tool(
    description=(
        "把微信聊天中识别出的需求、承诺、待办、风险、决策或跟进事项保存为待审核候选。"
        "不会自动把智能体推断提升为正式客户事实。"
    ),
    annotations=ADDITIVE_TOOL,
)
def save_customer_signal_candidate(
    customer_id: str,
    signal_type: str,
    statement: str,
    evidence_message_ids: list[str] | None = None,
    confidence: str = "low",
    status: str = "open",
    due_at: str | None = None,
) -> dict[str, Any]:
    customer = system().customers.get_customer(customer_id)
    if customer and customer["review_status"] != "approved":
        return envelope(
            "微信会话尚未确认为业务客户，未保存业务信号",
            {"customer_id": customer_id},
            status="warning",
            next_actions=["请用户先在本机管理台核对并确认客户身份"],
        )
    if signal_type not in {
        "requirement",
        "commitment",
        "todo",
        "risk",
        "decision",
        "follow_up",
        "preference",
    }:
        return envelope("客户业务信号类型无效", {"signal_type": signal_type}, status="error")
    if confidence not in {"low", "medium", "high"}:
        return envelope("置信度参数无效", {"confidence": confidence}, status="error")
    if status not in {"open", "done", "cancelled"}:
        return envelope("业务信号状态无效", {"status": status}, status="error")
    item = system().customers.create_signal(
        customer_id=customer_id,
        signal_type=signal_type,
        statement=statement,
        status=status,
        due_at=due_at,
        evidence_message_ids=evidence_message_ids or [],
        confidence=confidence,
    )
    if not item:
        return envelope("客户不存在", None, status="error")
    return envelope(
        "客户业务信号已保存为候选，等待用户审核",
        item,
        next_actions=["核对原始消息证据后在管理台批准或驳回"],
    )


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
