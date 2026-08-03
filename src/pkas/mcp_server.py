from typing import Any

from mcp.server import MCPServer

from pkas.system import KnowledgeSystem

mcp = MCPServer(
    "personal-knowledge-agent",
    title="个人知识与智能协作系统",
    description=(
        "为 Codex 提供本地个人知识检索、来源阅读、明确授权导入、"
        "智能体上下文准备、自我画像候选和蒸馏样本候选工具。"
    ),
    instructions=(
        "先检索再回答；重要结论保留 source_id、document_id、locator 和 original_uri。"
        "不要猜测未检索到的个人事实。导入资料必须先调用 inspect_import_path，"
        "只有用户明确批准完全相同的路径、领域和隐私范围后，才调用 import_confirmed_path。"
        "个人画像和蒸馏样本只能创建候选，批准操作由用户在管理台完成。"
        "微信客户聊天默认是 restricted；只有任务明确需要且用户授权时才读取。"
        "回复客户时只生成草稿，不直接发送微信消息。"
    ),
    version="0.1.0",
)


def system() -> KnowledgeSystem:
    return KnowledgeSystem.create()


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
        "检索本地个人知识库，返回带原始文件路径和文档定位的片段。默认不返回 restricted 资料。"
    )
)
def search_knowledge(
    query: str,
    domain: str | None = None,
    limit: int = 10,
    include_restricted: bool = False,
) -> dict[str, Any]:
    results = system().repository.search(
        query,
        domain=domain,
        limit=max(1, min(limit, 50)),
        include_restricted=include_restricted,
    )
    return envelope(f"找到 {len(results)} 条带来源的知识片段", results)


@mcp.tool(description="按 document_id 阅读知识库中的原文，可分页读取长文档。")
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


@mcp.tool(description="列出最近导入的本地资料来源及其领域、隐私级别和哈希。")
def list_sources(limit: int = 50) -> dict[str, Any]:
    items = system().repository.list_sources(max(1, min(limit, 200)))
    return envelope(f"已读取 {len(items)} 个资料来源", items)


@mcp.tool(
    description=(
        "只读检查一个用户明确给出的绝对路径，统计可导入、敏感和不支持文件；"
        "不会复制、解析或索引资料。正式导入前必须先调用本工具。"
    )
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
    )
)
def import_confirmed_path(
    path: str,
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
    if domain not in {"work", "self", "shared", "distill"}:
        return envelope("领域参数无效", {"domain": domain}, status="error")
    if privacy not in {"public", "private", "restricted"}:
        return envelope("隐私参数无效", {"privacy": privacy}, status="error")
    result = system().workflows.run_import(
        path=path,
        recursive=recursive,
        domain=domain,
        privacy=privacy,
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
        "为当前任务自动选择工作/自我知识范围并准备带证据的上下文。"
        "本工具负责检索与记录，最终推理和行动由调用它的 Codex 智能体完成。"
    )
)
def prepare_agent_context(
    task: str,
    domain: str | None = None,
    limit: int = 8,
    include_restricted: bool = False,
) -> dict[str, Any]:
    result = system().agent.prepare_context(
        task=task,
        domain=domain,
        limit=max(1, min(limit, 30)),
        include_restricted=include_restricted,
    )
    return envelope(result["summary"], result)


@mcp.tool(
    description=(
        "把有证据支持的性格、偏好或习惯保存为待审核候选。不会自动写入已批准的长期个人画像。"
    )
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
    description=("把一次高质量问答、决策或表达偏好保存为待审核蒸馏样本。不会自动批准或用于训练。")
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


@mcp.tool(description="列出已经由用户从 WeFlow 导出文件明确导入知识库的微信客户会话。")
def list_weflow_customers(limit: int = 100) -> dict[str, Any]:
    items = system().customers.list_customers(max(1, min(limit, 500)))
    return envelope(f"已读取 {len(items)} 个微信客户会话", items)


@mcp.tool(
    description=(
        "读取指定客户的微信时间线。微信聊天默认 restricted，"
        "必须在用户授权当前客户任务后显式设置 include_restricted=true。"
    )
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
        "在 WeFlow 客户聊天中检索需求、承诺、报价、进度或历史沟通。默认不检索 restricted 聊天。"
    )
)
def search_customer_messages(
    query: str,
    customer_id: str | None = None,
    limit: int = 20,
    include_restricted: bool = False,
) -> dict[str, Any]:
    items = system().customers.search_messages(
        query,
        customer_id=customer_id,
        limit=max(1, min(limit, 100)),
        include_restricted=include_restricted,
    )
    return envelope(
        f"找到 {len(items)} 条客户聊天证据",
        items,
        artifacts=list({item["vault_path"] for item in items if item.get("vault_path")}),
    )


@mcp.tool(
    description=(
        "为指定微信客户准备回复上下文：客户档案、近期聊天、历史匹配、"
        "已批准需求/承诺/待办和相关工作知识。只生成上下文和回复草稿依据，不发送消息。"
    )
)
def prepare_customer_reply_context(
    customer_id: str,
    task: str,
    recent_limit: int = 40,
    search_limit: int = 20,
    include_restricted: bool = False,
) -> dict[str, Any]:
    result = system().customer_service.prepare_reply_context(
        customer_id=customer_id,
        task=task,
        recent_limit=max(1, min(recent_limit, 200)),
        search_limit=max(1, min(search_limit, 100)),
        include_restricted=include_restricted,
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
    )
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
