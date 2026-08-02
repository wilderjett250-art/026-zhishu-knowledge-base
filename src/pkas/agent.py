from typing import Any

from pkas.repository import Repository

WORK_TERMS = {
    "业务",
    "项目",
    "代码",
    "开发",
    "服务器",
    "部署",
    "设备",
    "数据库",
    "接口",
    "客户",
    "合同",
    "故障",
    "测试",
    "报告",
}
SELF_TERMS = {
    "我",
    "自己",
    "性格",
    "偏好",
    "聊天",
    "习惯",
    "人生",
    "情绪",
    "价值观",
    "表达",
    "学习状态",
    "蒸馏",
}


class AgentService:
    def __init__(self, repository: Repository | None = None) -> None:
        self.repository = repository or Repository()

    @staticmethod
    def select_domain(task: str, requested_domain: str | None = None) -> str | None:
        if requested_domain:
            return requested_domain
        work_score = sum(1 for term in WORK_TERMS if term in task)
        self_score = sum(1 for term in SELF_TERMS if term in task)
        if work_score > self_score:
            return "work"
        if self_score > work_score:
            return "self"
        return None

    def prepare_context(
        self,
        *,
        task: str,
        domain: str | None,
        limit: int,
        include_restricted: bool,
    ) -> dict[str, Any]:
        selected_domain = self.select_domain(task, domain)
        plan = [
            "识别任务领域与隐私范围",
            "检索相关知识和原始证据",
            "返回可定位的上下文片段",
            "由调用方完成推理、执行和结果验证",
        ]
        results = self.repository.search(
            task,
            domain=selected_domain,
            limit=limit,
            include_restricted=include_restricted,
        )
        context = {
            "selected_domain": selected_domain or "all",
            "include_restricted": include_restricted,
            "results": results,
        }
        summary = (
            f"已为任务准备 {len(results)} 条上下文，检索范围为 {selected_domain or '全部领域'}"
        )
        run_id = self.repository.record_agent_run(
            task=task,
            selected_domain=selected_domain,
            plan=plan,
            context=context,
            result={"summary": summary},
        )
        return {
            "run_id": run_id,
            "task": task,
            "selected_domain": selected_domain,
            "plan": plan,
            "context": results,
            "summary": summary,
        }
