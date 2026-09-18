from typing import Any

from pkas.repository import Repository
from pkas.weflow import WeFlowService


class CustomerWorkflowService:
    def __init__(self, *, repository: Repository, weflow: WeFlowService) -> None:
        self.repository = repository
        self.weflow = weflow

    def import_weflow_exports(
        self,
        *,
        records_path: str | None,
        session_ids: list[str],
        inspection_token: str,
        privacy: str,
    ) -> dict[str, Any]:
        run_id = self.repository.create_workflow_run(
            "weflow_xlsx_import",
            {
                "records_path": records_path,
                "session_ids": session_ids,
                "privacy": privacy,
                "api_required": False,
                "key_accessed": False,
            },
        )
        try:
            inspect_step = self.repository.add_workflow_step(
                run_id,
                1,
                "verify_weflow_xlsx_selection",
            )
            inspection = self.weflow.inspect_export_selection(
                records_path=records_path,
                session_ids=session_ids,
            )
            if inspection["inspection_token"] != inspection_token:
                raise ValueError("WeFlow 导出记录或 XLSX 文件在检查后发生了变化。")
            self.repository.finish_workflow_step(
                inspect_step,
                status="completed",
                summary=(
                    f"已核对 {inspection['selected_sessions']} 个唯一 WeFlow XLSX，"
                    f"跳过 {inspection['skipped_alias_sessions']} 条重复别名"
                ),
            )

            import_step = self.repository.add_workflow_step(
                run_id,
                2,
                "import_selected_weflow_exports",
            )
            result = self.weflow.import_export_selection(
                records_path=records_path,
                session_ids=session_ids,
                inspection_token=inspection_token,
                privacy=privacy,
            )
            failed_sessions = result["failed_sessions"]
            if failed_sessions == 0:
                final_status = "completed"
            elif result["sessions"]:
                final_status = "warning"
            else:
                final_status = "failed"
            self.repository.finish_workflow_step(
                import_step,
                status=final_status,
                summary=(
                    f"导入 {len(result['sessions'])} 个待归类微信会话，"
                    f"新增 {result['imported']} 条消息，"
                    f"识别 {result['duplicates']} 条重复消息，失败 {failed_sessions} 个会话"
                ),
                artifacts=result["snapshot_paths"],
                error={"sessions": result["session_errors"]} if failed_sessions else None,
            )

            verify_step = self.repository.add_workflow_step(run_id, 3, "verify_customer_index")
            stats = self.repository.stats()
            self.repository.finish_workflow_step(
                verify_step,
                status="completed",
                summary=f"客户消息索引现有 {stats['counts']['customer_messages']} 条消息",
            )
            output = {"inspection": inspection, "result": result, "stats": stats}
            run_error = None
            if final_status == "failed":
                run_error = {
                    "type": "WeFlowBatchImportError",
                    "message": f"{failed_sessions} 个 WeFlow 会话均未能导入。",
                    "safe_retry": "查看失败会话原因，修复文件后重新检查并重试。",
                    "stop_condition": "导出文件缺失、损坏或会话范围发生变化时停止。",
                }
            self.repository.finish_workflow_run(
                run_id,
                status=final_status,
                output=output,
                error=run_error,
            )
            response = {
                "run_id": run_id,
                "status": final_status,
                "artifacts": result["snapshot_paths"],
                **output,
            }
            if run_error:
                response["error"] = run_error
            return response
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "safe_retry": "重新发现导出记录，检查同一批 XLSX 后再确认导入；消息会自动去重。",
                "stop_condition": "导出文件缺失、检查后变化或会话范围不明确时停止。",
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
