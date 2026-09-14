from typing import Any

from pkas.customer_repository import CustomerRepository
from pkas.repository import Repository


class CustomerReviewRequired(ValueError):
    """Raised when an unreviewed WeChat conversation enters a customer workflow."""


class CustomerService:
    def __init__(
        self,
        *,
        customers: CustomerRepository,
        knowledge: Repository,
    ) -> None:
        self.customers = customers
        self.knowledge = knowledge

    def prepare_reply_context(
        self,
        *,
        customer_id: str,
        task: str,
        recent_limit: int = 40,
        search_limit: int = 20,
        include_restricted: bool = False,
    ) -> dict[str, Any] | None:
        customer = self.customers.get_customer(customer_id)
        if not customer:
            return None
        if customer["review_status"] != "approved":
            raise CustomerReviewRequired(
                "该微信会话尚未由用户确认为业务客户，不能准备客户回复上下文。"
            )
        recent_messages = self.customers.timeline(
            customer_id,
            limit=recent_limit,
            include_restricted=include_restricted,
        )
        matched_messages = self.customers.search_messages(
            task,
            customer_id=customer_id,
            limit=search_limit,
            include_restricted=include_restricted,
        )
        signals = self.customers.list_signals(customer_id, limit=100)
        approved_signals = [item for item in signals if item["approval_status"] == "approved"]
        candidate_signals = [item for item in signals if item["approval_status"] == "candidate"]
        work_knowledge = self.knowledge.search(
            task,
            domain="work",
            limit=search_limit,
            include_restricted=include_restricted,
        )
        context = {
            "customer": customer,
            "recent_messages": recent_messages,
            "matched_messages": matched_messages,
            "approved_signals": approved_signals,
            "candidate_signals": candidate_signals,
            "work_knowledge": work_knowledge,
            "reply_rules": [
                "只使用检索到的客户事实，不补造承诺、价格或进度。",
                "区分客户原话、已批准业务信号和智能体推断。",
                "回复草稿默认由用户确认后再发送，不直接操作微信。",
                "引用重要承诺时保留 message_id 和原始快照路径。",
            ],
        }
        plan = [
            "确认客户身份与会话范围",
            "读取最近对话并检索历史相关消息",
            "核对已批准需求、承诺、待办和风险",
            "补充相关业务知识和项目资料",
            "生成带依据的回复草稿并等待用户确认",
        ]
        summary = (
            f"已为 {customer['display_name']} 准备 {len(recent_messages)} 条近期消息、"
            f"{len(matched_messages)} 条匹配历史和 {len(approved_signals)} 条已批准业务信号"
        )
        run_id = self.knowledge.record_agent_run(
            task=task,
            selected_domain="work",
            plan=plan,
            context=context,
            result={"summary": summary, "customer_id": customer_id},
        )
        return {
            "run_id": run_id,
            "task": task,
            "summary": summary,
            "plan": plan,
            **context,
        }
