import argparse
import json
from datetime import UTC, datetime
from typing import Any

from pkas.system import KnowledgeSystem


def run_agent_jobs(
    knowledge_system: KnowledgeSystem,
    *,
    max_jobs: int = 3,
    job_id: str | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    results: list[dict[str, Any]] = []
    if not knowledge_system.settings.deepseek_enabled:
        report = {
            "status": "warning",
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "processed": 0,
            "completed": 0,
            "failed": 0,
            "pending": 0,
            "reason": "deepseek_not_configured",
            "results": [],
        }
        return _write_report(knowledge_system, report)

    completed = 0
    failed = 0
    pending = 0
    deferred = 0
    for _ in range(max(1, min(max_jobs, 20))):
        job = knowledge_system.repository.claim_agent_job(job_id)
        if not job:
            break
        if job["job_type"] not in {"codex_closeout", "codex_daily_closeout"}:
            knowledge_system.repository.finish_agent_job(
                job["id"],
                status="skipped",
                error={"code": "unsupported_job", "message": "不支持的 Agent 任务类型。"},
            )
            results.append({"job_id": job["id"], "status": "skipped"})
            if job_id:
                break
            continue
        previous_result = json.loads(job["result_json"]) if job.get("result_json") else {}
        previous_run_id = previous_result.get("run_id")
        if previous_run_id:
            result = knowledge_system.agent.resume(previous_run_id)
        elif job["job_type"] == "codex_daily_closeout":
            source_ids = [str(item) for item in job["payload"].get("source_ids", []) if item]
            if not source_ids:
                knowledge_system.repository.finish_agent_job(
                    job["id"],
                    status="failed",
                    error={"code": "missing_sources", "message": "每日收尾任务缺少来源。"},
                )
                failed += 1
                results.append(
                    {"job_id": job["id"], "status": "failed", "error_code": "missing_sources"}
                )
                if job_id:
                    break
                continue
            result = knowledge_system.agent.closeout_codex_batch(
                source_ids=source_ids,
                workspace_path=job.get("workspace_path"),
            )
        else:
            if not job.get("source_id"):
                knowledge_system.repository.finish_agent_job(
                    job["id"],
                    status="failed",
                    error={"code": "missing_source", "message": "任务缺少来源。"},
                )
                failed += 1
                continue
            result = knowledge_system.agent.closeout_codex_turn(
                source_id=job["source_id"],
                workspace_path=job.get("workspace_path"),
            )
        if result["status"] == "completed":
            knowledge_system.repository.finish_agent_job(
                job["id"],
                status="completed",
                result={"run_id": result["run_id"]},
            )
            completed += 1
            results.append(
                {"job_id": job["id"], "status": "completed", "run_id": result["run_id"]}
            )
        else:
            error = result.get("error") or {"code": "agent_failed", "message": "Agent 未完成。"}
            budget_deferred = error.get("code") in {
                "daily_token_budget_exceeded",
                "deepseek_not_configured",
            }
            retry_later = bool(error.get("retryable")) or budget_deferred
            can_retry = retry_later and job["attempts"] < job["max_attempts"]
            next_status = "pending" if can_retry else "failed"
            if budget_deferred:
                knowledge_system.repository.defer_agent_job(
                    job["id"],
                    result={"run_id": result["run_id"]},
                    error=error,
                )
                next_status = "pending"
                deferred += 1
            else:
                knowledge_system.repository.finish_agent_job(
                    job["id"],
                    status=next_status,
                    result={"run_id": result["run_id"]},
                    error=error,
                )
            if next_status == "pending":
                pending += 1
            else:
                failed += 1
            results.append(
                {"job_id": job["id"], "status": next_status, "error_code": error.get("code")}
            )
            if budget_deferred:
                break
        if job_id:
            break

    report = {
        "status": "failed" if failed else ("warning" if pending else "completed"),
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "processed": len(results),
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "deferred": deferred,
        "results": results,
    }
    return _write_report(knowledge_system, report)


def _write_report(
    knowledge_system: KnowledgeSystem,
    report: dict[str, Any],
) -> dict[str, Any]:
    output_dir = knowledge_system.settings.data_root / "runs" / "agent"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_path = output_dir / f"agent-{stamp}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(output_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="处理待执行的 DeepSeek Agent 知识整理任务")
    parser.add_argument("--max-jobs", type=int, default=3)
    parser.add_argument("--job-id")
    args = parser.parse_args()
    result = run_agent_jobs(
        KnowledgeSystem.create(),
        max_jobs=args.max_jobs,
        job_id=args.job_id,
    )
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
