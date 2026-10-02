"""Small, local run receipts. Never persist source paths, prompts or exception text."""

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_ISSUES = 20
ACTION = {
    "root_failed": "检查该资料源是否仍存在且有读取权限，再手动重试；不要扩大扫描范围。",
    "root_partial": "在资料来源台账筛选失败项，按文件原因修复权限或解析后重试。",
    "classification_retry": "文件可能正在写入或暂不可读；保持待复查，稳定后重新同步。",
    "index_outbox": "检查语义服务和待处理队列；全文检索不受此项直接影响。",
    "no_registered_roots": "先在资料接入中登记并启用本地资料源；定时任务不会自行扩大扫描范围。",
    "weflow_export_failed": "检查 WeFlow 夜间导出任务状态；本地文件同步仍会继续。",
    "low_disk_space": "资料盘空间不足，已暂停处理；释放空间后重试，不会因重复运行而恢复。",
    "weflow_failed": "检查已授权的导出目录与最近一次导出，再重试增量导入。",
    "weflow_partial": "查看微信增量导入失败会话数；不要把部分成功当作全部完成。",
    "weflow_retention_warning": (
        "聊天已导入，但旧导出副本未能安全清理；核查受管导出目录、入库快照与权限。"
    ),
    "worker_failed": "检查本机任务配置和可用磁盘空间；仍失败时查看本机受限服务日志。",
    "worker_config": "检查同步任务配置、授权范围和依赖路径；修正后重试。",
    "worker_io": "检查数据盘空间、目录权限和文件占用；修正后重试。",
    "worker_runtime_missing": "同步所需的 Python 环境不可用；修复知域运行环境后重试。",
    "worker_initialization": "同步程序初始化失败；按异常类型检查配置、数据库或运行环境。",
    "sync_state_failed": "同步进度未能保存；检查配置目录权限和磁盘空间，重试时会按消息去重。",
    "sync_schedule_failed": (
        "补跑检查或一次性任务配置失败；检查每日同步进度文件与当前用户计划任务。"
    ),
    "sync_storage_unavailable": "资料盘不可访问；恢复已配置的资料盘后重试，不要改用系统盘。",
    "sync_receipt_failed": "诊断文件未能保存；检查日志目录权限和空间，并查看计划任务退出码。",
    "sync_history_reset": "诊断历史损坏或超过容量限制，已重置历史；本次异常仍已保存。",
    "weflow_authorization_failed": (
        "微信同步授权配置无法读取或格式错误；修复授权配置，任务不会擅自启动导出。"
    ),
    "weflow_export_environment": (
        "WeFlow 或其 Node/Electron 环境不可用；修复已配置的导出环境后重试。"
    ),
    "weflow_export_directory": (
        "微信受管导出目录不可用或不安全；检查资料盘和普通目录，勿改用系统盘。"
    ),
    "weflow_export_low_disk": "微信导出盘不足 2 GiB；释放空间后重试，不会推进导入水位线。",
    "weflow_export_quota": "受管微信导出副本超过 4 GiB；先核对入库与安全清理，避免继续积累。",
    "weflow_configuration_failed": "WeFlow 自动导出配置失败；检查配置权限与当前账号的任务设置。",
    "weflow_task_mismatch": (
        "已打开的 WeFlow 未使用本轮受管目录或任务未启用；保存工作后重启 WeFlow。"
    ),
    "weflow_launch_failed": "WeFlow 后台进程未能启动；检查已配置的运行环境后重试。",
    "weflow_config_unreadable": (
        "等待导出期间无法读取 WeFlow 任务状态；检查配置文件格式、权限和写入占用。"
    ),
    "weflow_export_timeout": "等待微信导出超时；查看 WeFlow 自动导出状态，确认完成后重试增量导入。",
    "weflow_schedule_not_due": (
        "WeFlow 的下次触发晚于本轮等待上限；其间隔任务可能与零点同步错位。"
        "保存工作后退出 WeFlow，让已授权定时任务重新启动；已有导出仍会增量导入。"
    ),
    "weflow_process_exited": (
        "本轮启动的 WeFlow 在导出完成前退出；检查导出环境后重试，已有导出仍会导入。"
    ),
    "weflow_export_sessions": (
        "WeFlow 无法读取当前会话列表；检查其连接状态后重试，不代表没有新消息。"
    ),
    "weflow_export_partial": "微信导出有会话失败；已完成的导出仍可导入，失败会话需要重试。",
    "weflow_catalog_failed": "微信导出记录无法读取或不完整；检查导出台账，暂不推进导入水位线。",
    "weflow_inspection_failed": (
        "微信 XLSX 检查失败；按异常类型检查文件格式、权限或文件变化后重新导入。"
    ),
    "weflow_import_failed": "微信消息入库失败；检查数据库、原文仓与文件权限，重试会按消息去重。",
    "weflow_import_format": (
        "有微信导出文件格式错误、损坏或包含内嵌媒体；重新导出纯聊天 XLSX 后重试。"
    ),
    "weflow_import_boundary": (
        "微信导出在检查后发生变化或不符合授权边界；重新检查同一批文件后重试。"
    ),
    "weflow_import_database": "微信消息写入数据库失败；检查空间、数据库可用性和文件占用后重试。",
    "weflow_import_io": "微信导出或原文仓无法读取/写入；检查文件存在性、权限和磁盘空间后重试。",
    "weflow_cleanup_failed": (
        "同步已结束，但本轮启动的后台进程未能退出；检查后台进程，勿终止用户原有实例。"
    ),
}

STAGE = {
    **dict.fromkeys(("worker_config", "worker_runtime_missing"), "环境检查"),
    "worker_initialization": "同步初始化",
    "weflow_authorization_failed": "同步授权",
    **dict.fromkeys(
        (
            "weflow_export_environment",
            "weflow_configuration_failed",
            "weflow_task_mismatch",
            "weflow_launch_failed",
        ),
        "导出准备",
    ),
    **dict.fromkeys(
        ("weflow_export_directory", "weflow_export_low_disk", "weflow_export_quota"), "导出空间检查"
    ),
    **dict.fromkeys(
        (
            "weflow_export_failed",
            "weflow_export_timeout",
            "weflow_schedule_not_due",
            "weflow_process_exited",
            "weflow_config_unreadable",
            "weflow_export_sessions",
            "weflow_export_partial",
        ),
        "等待消息导出",
    ),
    "weflow_catalog_failed": "发现新增导出",
    "weflow_inspection_failed": "导出文件检查",
    **dict.fromkeys(
        (
            "weflow_import_failed",
            "weflow_import_format",
            "weflow_import_boundary",
            "weflow_import_database",
            "weflow_import_io",
            "weflow_failed",
            "weflow_partial",
        ),
        "消息入库",
    ),
    "sync_state_failed": "保存同步进度",
    "sync_schedule_failed": "开机补跑检查",
    "sync_storage_unavailable": "入库空间检查",
    "low_disk_space": "空间检查",
    "weflow_retention_warning": "清理导出副本",
    "weflow_cleanup_failed": "回收后台进程",
    "sync_receipt_failed": "保存运行日志",
    "sync_history_reset": "保存运行日志",
}
EXCEPTION_TYPES = frozenset(
    {
        "ValueError",
        "TypeError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "RuntimeError",
        "JSONDecodeError",
        "ValidationError",
        "ImportError",
        "ModuleNotFoundError",
        "DatabaseError",
        "OperationalError",
        "IntegrityError",
        "WeFlowFormatError",
        "ImportBoundaryError",
        "BadZipFile",
    }
)


def _atomic_write(path: Path, value: bytes) -> None:
    """Keep failure reporting independent of application/model dependencies."""
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def issue(
    code: str,
    count: int = 1,
    root_id: str | None = None,
    *,
    error_type: str | None = None,
) -> dict[str, Any]:
    if code not in ACTION:
        raise ValueError("未知诊断代码")
    item: dict[str, Any] = {"code": code, "count": max(1, int(count)), "action": ACTION[code]}
    if code in STAGE:
        item["stage"] = STAGE[code]
    if isinstance(error_type, str) and error_type in EXCEPTION_TYPES:
        item["error_type"] = error_type
    if (
        isinstance(root_id, str)
        and root_id.isascii()
        and root_id.replace("_", "").isalnum()
        and len(root_id) < 80
    ):
        item["root_id"] = root_id
    return item


def root_scan_issues(result: dict[str, Any], root_id: str) -> list[dict[str, Any]]:
    issues = []
    file_errors = int(result.get("errors") or 0) + int(result.get("unreadable") or 0)
    if file_errors:
        issues.append(issue("root_partial", file_errors, root_id))
    classification_errors = int(result.get("classification_failed") or 0)
    if classification_errors:
        issues.append(issue("classification_retry", classification_errors, root_id))
    return issues


def _read(path: Path, default: Any) -> Any:
    try:
        if path.stat().st_size > 1024 * 1024:
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def write_run_receipt(
    data_root: Path,
    *,
    kind: str,
    status: str,
    counts: dict[str, int],
    issues: list[dict[str, Any]],
    run_id: str | None = None,
) -> Path:
    if kind not in {"sync", "scheduled-sync"}:
        raise ValueError("未知运行类型")
    home = data_root / "runs" / "diagnostics" / kind
    home.mkdir(parents=True, exist_ok=True)
    receipt = {
        "version": 1,
        "run_id": uuid.UUID(run_id).hex if run_id else uuid.uuid4().hex,
        "kind": kind,
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": status if status in {"completed", "warning", "failed", "deferred"} else "failed",
        "counts": {key: max(0, int(value)) for key, value in counts.items() if key.isidentifier()},
        "issues": [
            issue(
                str(item.get("code")),
                int(item.get("count", 1)),
                item.get("root_id"),
                error_type=item.get("error_type"),
            )
            for item in issues[:10]
        ],
    }
    latest = home / "latest.json"
    _atomic_write(latest, json.dumps(receipt, ensure_ascii=False).encode("utf-8"))
    if receipt["issues"]:
        history = _read(home / "recent-issues.json", [])
        if not isinstance(history, list):
            history = []
        # Fixed-size history. Existing legacy per-run logs are left untouched.
        history = ([receipt] + [row for row in history if isinstance(row, dict)])[:MAX_ISSUES]
        _atomic_write(
            home / "recent-issues.json",
            json.dumps(history, ensure_ascii=False).encode("utf-8"),
        )
    return latest


def _combine_receipts(rows: list[Any]) -> list[dict[str, Any]]:
    valid = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("recorded_at"), str)
        and isinstance(row.get("issues"), list)
        and isinstance(row.get("counts"), dict)
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for row in sorted(valid, key=lambda item: item["recorded_at"]):
        row = {
            **row,
            "issues": [
                issue(
                    str(item["code"]),
                    int(item.get("count", 1)),
                    item.get("root_id"),
                    error_type=item.get("error_type"),
                )
                for item in row["issues"][:10]
                if isinstance(item, dict)
                and isinstance(item.get("code"), str)
                and item["code"] in ACTION
                and isinstance(item.get("count", 1), int)
            ],
        }
        key = str(row.get("run_id") or row["recorded_at"])
        previous = grouped.get(key)
        if previous is None:
            grouped[key] = row
            continue
        severity = {"completed": 0, "deferred": 0, "warning": 1, "failed": 2}
        all_issues = {
            str(item.get("code")): item
            for item in previous["issues"] + row["issues"]
            if isinstance(item, dict)
        }
        grouped[key] = {
            **previous,
            **row,
            "counts": {**previous.get("counts", {}), **row.get("counts", {})},
            "status": max(
                (previous.get("status"), row.get("status")),
                key=lambda status: severity.get(str(status), 2),
            ),
            "issues": list(all_issues.values())[:10],
        }
    return sorted(grouped.values(), key=lambda row: row["recorded_at"], reverse=True)


def read_run_receipts(data_root: Path) -> dict[str, Any]:
    home = data_root / "runs" / "diagnostics"
    result = {}
    for kind in ("sync", "scheduled-sync"):
        recent = _read(home / kind / "recent-issues.json", [])
        latest = _read(home / kind / "latest.json", None)
        latest_rows = [latest]
        recent_rows = recent if isinstance(recent, list) else []
        if kind == "scheduled-sync":
            # The PowerShell fallback works even when Python cannot start.
            # Separate files avoid two writers overwriting each other's result.
            latest_rows.append(_read(home / kind / "launcher-latest.json", None))
            launcher_recent = _read(home / kind / "launcher-recent-issues.json", [])
            if isinstance(launcher_recent, list):
                recent_rows = recent_rows + launcher_recent
        combined_latest = _combine_receipts(latest_rows)
        result[kind] = {
            "latest": combined_latest[0] if combined_latest else None,
            "recent_issues": _combine_receipts(recent_rows)[:MAX_ISSUES],
        }
    return result
