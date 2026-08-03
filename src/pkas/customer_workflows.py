from typing import Any

from pkas.repository import Repository
from pkas.weflow import WeFlowService


class CustomerWorkflowService:
    def __init__(self, *, repository: Repository, weflow: WeFlowService) -> None:
        self.repository = repository
        self.weflow = weflow

    def sync_weflow(
        self,
        *,
        base_url: str,
        access_token: str,
        session_ids: list[str],
        incremental: bool,
        privacy: str,
        max_messages_per_session: int,
    ) -> dict[str, Any]:
        run_id = self.repository.create_workflow_run(
            "weflow_customer_sync",
            {
                "base_url": base_url,
                "session_ids": session_ids,
                "incremental": incremental,
                "privacy": privacy,
                "max_messages_per_session": max_messages_per_session,
                "token_stored": False,
            },
        )
        try:
            connect_step = self.repository.add_workflow_step(run_id, 1, "connect_weflow")
            health = self.weflow.check_connection(
                base_url=base_url,
                access_token=access_token,
            )
            self.repository.finish_workflow_step(
                connect_step,
                status="completed",
                summary="WeFlow 本地 API 鉴权和健康检查通过",
            )

            sync_step = self.repository.add_workflow_step(run_id, 2, "sync_selected_sessions")
            result = self.weflow.sync_sessions(
                base_url=base_url,
                access_token=access_token,
                session_ids=session_ids,
                incremental=incremental,
                privacy=privacy,
                max_messages_per_session=max_messages_per_session,
            )
            self.repository.finish_workflow_step(
                sync_step,
                status="completed",
                summary=(
                    f"同步 {len(result['sessions'])} 个会话，新增 {result['imported']} 条消息，"
                    f"识别 {result['duplicates']} 条重复消息"
                ),
                artifacts=result["snapshot_paths"],
            )

            verify_step = self.repository.add_workflow_step(run_id, 3, "verify_customer_index")
            stats = self.repository.stats()
            self.repository.finish_workflow_step(
                verify_step,
                status="completed",
                summary=f"客户消息索引现有 {stats['counts']['customer_messages']} 条消息",
            )
            output = {"health": health, "sync": result, "stats": stats}
            self.repository.finish_workflow_run(run_id, status="completed", output=output)
            return {
                "run_id": run_id,
                "status": "completed",
                "artifacts": result["snapshot_paths"],
                **output,
            }
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "safe_retry": "检查 WeFlow API、Token 和已选会话后重试；增量去重不会重复写入消息。",
                "stop_condition": "会话范围不明确、Token 暴露或本机 API 身份无法确认时停止。",
            }
            self.repository.finish_workflow_run(run_id, status="failed", error=error)
            return {"run_id": run_id, "status": "failed", "error": error, "artifacts": []}

    def import_chatlab(
        self,
        *,
        path: str,
        inspection_token: str,
        session_id: str | None,
        privacy: str,
    ) -> dict[str, Any]:
        run_id = self.repository.create_workflow_run(
            "weflow_chatlab_import",
            {
                "path": path,
                "session_id": session_id,
                "privacy": privacy,
            },
        )
        try:
            step_id = self.repository.add_workflow_step(run_id, 1, "import_chatlab_snapshot")
            result = self.weflow.import_chatlab_file(
                path=path,
                inspection_token=inspection_token,
                session_id=session_id,
                privacy=privacy,
            )
            self.repository.finish_workflow_step(
                step_id,
                status="completed",
                summary=(
                    f"导入 {result['imported']} 条 WeFlow 消息，"
                    f"识别 {result['duplicates']} 条重复消息"
                ),
                artifacts=[result["snapshot_path"]],
            )
            self.repository.finish_workflow_run(run_id, status="completed", output=result)
            return {
                "run_id": run_id,
                "status": "completed",
                "artifacts": [result["snapshot_path"]],
                "result": result,
            }
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "safe_retry": "重新检查同一个 ChatLab 文件和会话 ID 后再确认导入。",
                "stop_condition": "文件不是 WeFlow ChatLab JSON 或来源范围不明确时停止。",
            }
            self.repository.finish_workflow_run(run_id, status="failed", error=error)
            return {"run_id": run_id, "status": "failed", "error": error, "artifacts": []}
