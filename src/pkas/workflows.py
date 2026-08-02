import sqlite3
from typing import Any

from pkas.ingest import IngestionService
from pkas.repository import Repository


class WorkflowService:
    def __init__(
        self,
        repository: Repository | None = None,
        ingestion: IngestionService | None = None,
    ) -> None:
        self.repository = repository or Repository()
        self.ingestion = ingestion or IngestionService(repository=self.repository)

    def run_import(
        self,
        *,
        path: str,
        recursive: bool,
        domain: str,
        privacy: str,
        inspection_token: str | None = None,
    ) -> dict[str, Any]:
        input_data = {
            "path": path,
            "recursive": recursive,
            "domain": domain,
            "privacy": privacy,
        }
        run_id = self.repository.create_workflow_run("import_path", input_data)
        artifacts: list[str] = []
        try:
            inspect_step = self.repository.add_workflow_step(run_id, 1, "inspect_source")
            inspection = self.ingestion.inspect_path(path, recursive)
            self.repository.finish_workflow_step(
                inspect_step,
                status="completed",
                summary=f"发现 {inspection['supported_files']} 个可导入文件",
                artifacts=[inspection["path"]],
            )

            import_step = self.repository.add_workflow_step(run_id, 2, "import_and_index")
            result = self.ingestion.import_path(
                path,
                recursive=recursive,
                domain=domain,
                privacy=privacy,
                inspection_token=inspection_token,
            )
            artifacts = [item["vault_path"] for item in result["items"] if item.get("vault_path")]
            step_status = "completed" if result["errors"] == 0 else "warning"
            self.repository.finish_workflow_step(
                import_step,
                status=step_status,
                summary=(
                    f"导入 {result['imported']} 项，重复 {result['duplicates']} 项，"
                    f"错误 {result['errors']} 项"
                ),
                artifacts=artifacts,
                error={"items": result["error_items"]} if result["errors"] else None,
            )

            verify_step = self.repository.add_workflow_step(run_id, 3, "verify_index")
            stats = self.repository.stats()
            self.repository.finish_workflow_step(
                verify_step,
                status="completed",
                summary=f"知识库现有 {stats['counts']['chunks']} 个检索片段",
            )

            final_status = "completed" if result["errors"] == 0 else "warning"
            output = {"inspection": inspection, "result": result, "stats": stats}
            self.repository.finish_workflow_run(run_id, status=final_status, output=output)
            return {
                "run_id": run_id,
                "status": final_status,
                "artifacts": artifacts,
                **output,
            }
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "safe_retry": "确认来源路径和文件权限后，使用相同参数重新运行。",
                "stop_condition": "路径范围不明确或包含敏感资料时停止导入。",
            }
            self.repository.finish_workflow_run(run_id, status="failed", error=error)
            return {
                "run_id": run_id,
                "status": "failed",
                "error": error,
                "artifacts": artifacts,
            }

    def rebuild_search_index(self) -> dict[str, Any]:
        run_id = self.repository.create_workflow_run("rebuild_search_index", {})
        step_id = self.repository.add_workflow_step(run_id, 1, "rebuild_fts")
        database = self.repository.database
        try:
            with database.connect() as connection:
                connection.execute("DELETE FROM chunks_fts")
                connection.execute(
                    """
                    INSERT INTO chunks_fts(chunk_id, title, content, domain, privacy)
                    SELECT id, title, text_content, domain, privacy FROM chunks
                    """
                )
                count = connection.execute("SELECT COUNT(*) AS count FROM chunks_fts").fetchone()[
                    "count"
                ]
                connection.commit()
            output = {"indexed_chunks": count}
            self.repository.finish_workflow_step(
                step_id,
                status="completed",
                summary=f"重建 {count} 个全文检索片段",
            )
            self.repository.finish_workflow_run(run_id, status="completed", output=output)
            return {"run_id": run_id, "status": "completed", **output}
        except sqlite3.Error as exc:
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "safe_retry": "检查 SQLite 数据库完整性后重新运行索引重建。",
                "stop_condition": "数据库完整性检查失败时停止。",
            }
            self.repository.finish_workflow_step(
                step_id,
                status="failed",
                summary="全文索引重建失败",
                error=error,
            )
            self.repository.finish_workflow_run(run_id, status="failed", error=error)
            return {"run_id": run_id, "status": "failed", "error": error}

    def list_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "import_path",
                "title": "资料导入与索引",
                "steps": ["检查来源", "复制原件并解析", "建立索引", "验证结果"],
                "approval": "导入路径必须由用户明确指定",
            },
            {
                "name": "rebuild_search_index",
                "title": "全文索引重建",
                "steps": ["清理可重建索引", "重新索引", "核对片段数量"],
                "approval": "仅修改可重建索引",
            },
            {
                "name": "persona_review",
                "title": "个人观察审核",
                "steps": ["查看证据", "检查反例", "用户批准或驳回", "记录审计"],
                "approval": "长期个人特征必须由用户决定",
            },
        ]
