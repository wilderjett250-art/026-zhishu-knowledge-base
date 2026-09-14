import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pkas.codex_capture import is_internal_codex_turn
from pkas.repository import CODEX_USER_TASK_MIGRATION_KEY

if TYPE_CHECKING:
    from pkas.system import KnowledgeSystem


DAILY_CLOSEOUT_CURSOR_KEY = "agent_daily_closeout_last_cutoff"

_MATERIAL_PATTERNS = {
    "implementation_request": re.compile(
        r"(?:^|\n|请|帮我|麻烦|需要|继续|开始|直接|把|将|给我).{0,12}"
        r"(?:实现|开发|修复|修改|新增|添加|创建|更新|删除|重构|配置|接入|迁移|导入|同步)",
        re.MULTILINE,
    ),
    "delivery_request": re.compile(
        r"(?:^|\n|请|帮我|麻烦|需要|继续|开始|直接|把|将|给我).{0,12}"
        r"(?:测试|构建|编译|检查|验证|验收|部署|发布|上传|交付|生成)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "english_task_request": re.compile(
        r"(?:^|\n|please\s+|can you\s+|need to\s+)"
        r"(?:implement|fix|modify|update|create|add|remove|refactor|test|build|deploy|publish)\b",
        re.IGNORECASE,
    ),
    "git_task_request": re.compile(
        r"(?:^|\n|请|帮我|需要|继续|开始|直接).{0,12}"
        r"(?:git\s+(?:commit|push|merge|rebase)|提交|合并|推送)",
        re.IGNORECASE,
    ),
}


def material_signals(text: str) -> list[str]:
    return [name for name, pattern in _MATERIAL_PATTERNS.items() if pattern.search(text)]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _initial_window_start(now: datetime, timezone_name: str) -> datetime:
    local_timezone = datetime.now().astimezone().tzinfo or UTC
    if timezone_name.strip().lower() == "local":
        timezone = local_timezone
    else:
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone = local_timezone
    local_now = now.astimezone(timezone)
    return local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _window_start(
    knowledge_system: "KnowledgeSystem",
    now: datetime,
) -> datetime:
    saved = knowledge_system.repository.get_app_meta(DAILY_CLOSEOUT_CURSOR_KEY)
    if saved:
        try:
            parsed = _as_utc(datetime.fromisoformat(saved))
            if parsed <= now:
                return parsed
        except ValueError:
            pass
    return _initial_window_start(now, knowledge_system.settings.agent_daily_timezone)


def plan_daily_closeouts(
    knowledge_system: "KnowledgeSystem",
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved_now = _as_utc(now or datetime.now(UTC))
    if not knowledge_system.settings.agent_daily_closeout_enabled:
        return {
            "status": "disabled",
            "window_end": resolved_now.isoformat(),
            "queued": 0,
        }
    if (
        knowledge_system.repository.get_app_meta(CODEX_USER_TASK_MIGRATION_KEY)
        != "completed"
    ):
        return {
            "status": "deferred",
            "reason": "codex_user_task_migration_incomplete",
            "window_end": resolved_now.isoformat(),
            "queued": 0,
        }

    start = _window_start(knowledge_system, resolved_now)
    turns = knowledge_system.repository.list_codex_turns_ingested_between(
        start_at=start.isoformat(),
        end_at=resolved_now.isoformat(),
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_internal = 0
    for turn in turns:
        if is_internal_codex_turn(
            user_text=turn["text_content"],
            cwd=str(turn["metadata"].get("cwd") or ""),
        ):
            skipped_internal += 1
            continue
        metadata = turn["metadata"]
        thread_id = str(metadata.get("thread_id") or turn["source_id"])
        grouped[thread_id].append(turn)

    queued = 0
    deduplicated = 0
    skipped_non_material = 0
    planned_jobs: list[dict[str, Any]] = []
    max_sources_per_thread = max(
        1, knowledge_system.settings.agent_daily_max_sources_per_thread
    )
    selected_by_thread: list[tuple[str, list[dict[str, Any]], list[str]]] = []
    for thread_id, thread_turns in grouped.items():
        signals = sorted(
            {
                signal
                for turn in thread_turns
                for signal in material_signals(turn["text_content"])
            }
        )
        if not signals:
            skipped_non_material += 1
            continue
        selected_by_thread.append(
            (thread_id, thread_turns[-max_sources_per_thread:], signals)
        )

    selected_turns = [
        turn
        for _thread_id, thread_turns, _signals in selected_by_thread
        for turn in thread_turns
    ]
    max_sources_total = max(1, knowledge_system.settings.agent_daily_max_sources_total)
    omitted_source_count = max(0, len(selected_turns) - max_sources_total)
    selected_turns = selected_turns[-max_sources_total:]
    source_ids = [turn["source_id"] for turn in selected_turns]
    if source_ids:
        dedupe_material = "\n".join(source_ids).encode("utf-8")
        dedupe_key = hashlib.sha256(dedupe_material).hexdigest()
        latest = selected_turns[-1]
        workspaces = {
            str(turn["metadata"].get("cwd"))
            for turn in selected_turns
            if turn["metadata"].get("cwd")
        }
        workspace_path = next(iter(workspaces)) if len(workspaces) == 1 else None
        all_signals = sorted(
            {
                signal
                for _thread_id, _thread_turns, signals in selected_by_thread
                for signal in signals
            }
        )
        payload = {
            "capture_mode": "daily-batch-v2",
            "dedupe_key": dedupe_key,
            "thread_count": len(selected_by_thread),
            "source_ids": source_ids,
            "source_count": len(source_ids),
            "omitted_source_count": omitted_source_count,
            "material_signals": all_signals,
            "window_start": start.isoformat(),
            "window_end": resolved_now.isoformat(),
        }
        job = knowledge_system.repository.enqueue_agent_job(
            job_type="codex_daily_closeout",
            source_id=latest["source_id"],
            workspace_path=workspace_path,
            payload=payload,
            priority=10,
        )
        if "payload_json" in job:
            deduplicated += 1
        else:
            queued += 1
        planned_jobs.append(
            {
                "job_id": job["id"],
                "thread_count": len(selected_by_thread),
                "source_count": len(source_ids),
                "omitted_source_count": omitted_source_count,
                "material_signals": all_signals,
                "status": job["status"],
            }
        )

    consolidated_legacy = knowledge_system.repository.consolidate_pending_notify_closeouts()
    consolidated_daily = (
        knowledge_system.repository.consolidate_pending_legacy_daily_closeouts()
    )
    knowledge_system.repository.set_app_meta(
        DAILY_CLOSEOUT_CURSOR_KEY,
        resolved_now.isoformat(),
    )
    return {
        "status": "completed",
        "window_start": start.isoformat(),
        "window_end": resolved_now.isoformat(),
        "turns_seen": len(turns),
        "thread_groups": len(grouped),
        "queued": queued,
        "deduplicated": deduplicated,
        "skipped_internal": skipped_internal,
        "skipped_non_material": skipped_non_material,
        "consolidated_legacy_jobs": consolidated_legacy,
        "consolidated_legacy_daily_jobs": consolidated_daily,
        "jobs": planned_jobs,
    }
