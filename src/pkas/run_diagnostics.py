"""Small, local run receipts. Never persist source paths, prompts or exception text."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.content_taxonomy import atomic_write

MAX_ISSUES = 20
ACTION = {
    "root_failed": "检查该资料源是否仍存在且有读取权限，再手动重试；不要扩大扫描范围。",
    "root_partial": "在资料来源台账筛选失败项，按文件原因修复权限或解析后重试。",
    "classification_retry": "文件可能正在写入或暂不可读；保持待复查，稳定后重新同步。",
    "index_outbox": "检查语义服务和待处理队列；全文检索不受此项直接影响。",
    "no_registered_roots": "先在资料接入中登记并启用本地资料源；定时任务不会自行扩大扫描范围。",
    "weflow_export_failed": "检查 WeFlow 夜间导出任务状态；本地文件同步仍会继续。",
    "low_disk_space": "资料盘空间不足，已暂停余下资料源；清理或扩容后再运行，不要反复重试。",
    "weflow_failed": "检查已授权的导出目录与最近一次导出，再重试增量导入。",
    "weflow_partial": "查看微信增量导入失败会话数；不要把部分成功当作全部完成。",
    "weflow_retention_warning": (
        "聊天已导入，但旧导出副本未能安全清理；核查受管导出目录、入库快照与权限。"
    ),
    "worker_failed": "检查本机任务配置和可用磁盘空间；仍失败时查看本机受限服务日志。",
    "worker_config": "检查同步任务配置、授权范围和依赖路径；修正后重试。",
    "worker_io": "检查数据盘空间、目录权限和文件占用；修正后重试。",
}


def issue(code: str, count: int = 1, root_id: str | None = None) -> dict[str, Any]:
    if code not in ACTION:
        raise ValueError("未知诊断代码")
    item: dict[str, Any] = {"code": code, "count": max(1, int(count)), "action": ACTION[code]}
    if root_id and root_id.isascii() and root_id.replace("_", "").isalnum() and len(root_id) < 80:
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
) -> Path:
    if kind not in {"sync", "scheduled-sync"}:
        raise ValueError("未知运行类型")
    home = data_root / "runs" / "diagnostics" / kind
    home.mkdir(parents=True, exist_ok=True)
    receipt = {
        "version": 1,
        "kind": kind,
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": status if status in {"completed", "warning", "failed", "deferred"} else "failed",
        "counts": {key: max(0, int(value)) for key, value in counts.items() if key.isidentifier()},
        "issues": [
            issue(str(item.get("code")), int(item.get("count", 1)), item.get("root_id"))
            for item in issues[:10]
        ],
    }
    latest = home / "latest.json"
    atomic_write(latest, json.dumps(receipt, ensure_ascii=False).encode("utf-8"))
    if receipt["issues"]:
        history = _read(home / "recent-issues.json", [])
        if not isinstance(history, list):
            history = []
        # Fixed-size history. Existing legacy per-run logs are left untouched.
        history = ([receipt] + [row for row in history if isinstance(row, dict)])[:MAX_ISSUES]
        atomic_write(
            home / "recent-issues.json",
            json.dumps(history, ensure_ascii=False).encode("utf-8"),
        )
    return latest


def read_run_receipts(data_root: Path) -> dict[str, Any]:
    home = data_root / "runs" / "diagnostics"
    result = {}
    for kind in ("sync", "scheduled-sync"):
        recent = _read(home / kind / "recent-issues.json", [])
        latest = _read(home / kind / "latest.json", None)
        result[kind] = {
            "latest": latest if isinstance(latest, dict) else None,
            "recent_issues": [
                row for row in recent
                if isinstance(row, dict) and isinstance(row.get("recorded_at"), str)
                and isinstance(row.get("issues"), list)
            ][:MAX_ISSUES] if isinstance(recent, list) else [],
        }
    return result
