"""Local aggregate telemetry: never store prompts, paths, identities or tool arguments."""

import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path


class UsageMetrics:
    def __init__(self, data_root: Path):
        self.path = data_root / "runtime" / "usage.sqlite"

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        connection.execute("""CREATE TABLE IF NOT EXISTS usage (
            hour TEXT NOT NULL, channel TEXT NOT NULL, operation TEXT NOT NULL,
            outcome TEXT NOT NULL, calls INTEGER NOT NULL, duration_ms REAL NOT NULL,
            PRIMARY KEY(hour,channel,operation,outcome))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS usage_failures (
            hour TEXT NOT NULL, channel TEXT NOT NULL, operation TEXT NOT NULL,
            reason TEXT NOT NULL, calls INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
            PRIMARY KEY(hour,channel,operation,reason))""")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(
        self,
        channel: str,
        operation: str,
        outcome: str,
        elapsed: float,
        reason: str | None = None,
    ):
        """Record a small aggregate, never a prompt, path, argument or exception text."""
        try:
            with self._connect() as connection:
                now = datetime.now(UTC)
                connection.execute(
                    """INSERT INTO usage VALUES(?,?,?,?,1,?)
                    ON CONFLICT(hour,channel,operation,outcome) DO UPDATE SET
                    calls=calls+1,duration_ms=duration_ms+excluded.duration_ms""",
                    (
                        now.strftime("%Y-%m-%dT%H"),
                        channel,
                        operation,
                        outcome,
                        max(0, elapsed * 1000),
                    ),
                )
                if outcome == "error":
                    safe_reason = self._safe_failure_reason(reason)
                    connection.execute(
                        """INSERT INTO usage_failures VALUES(?,?,?,?,1,?)
                        ON CONFLICT(hour,channel,operation,reason) DO UPDATE SET
                        calls=calls+1,last_seen_at=excluded.last_seen_at""",
                        (
                            now.strftime("%Y-%m-%dT%H"),
                            channel,
                            operation,
                            safe_reason,
                            now.isoformat(),
                        ),
                    )
                connection.execute(
                    "DELETE FROM usage WHERE hour < ?",
                    ((now - timedelta(days=30)).strftime("%Y-%m-%dT%H"),),
                )
                connection.execute(
                    "DELETE FROM usage_failures WHERE hour < ?",
                    ((now - timedelta(days=30)).strftime("%Y-%m-%dT%H"),),
                )
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning("Usage aggregate write unavailable")

    @staticmethod
    def _safe_failure_reason(reason: str | None) -> str:
        # Callers pass one of these coarse categories.  Keeping a fixed allow
        # list prevents exception text, request values, identities and paths
        # from ever entering the local telemetry table.
        allowed = {
            "请求条件不满足或权限被拒绝",
            "本机服务未能完成请求",
            "本机服务出现未处理异常",
            "工具返回失败状态",
            "工具执行异常",
        }
        return reason if reason in allowed else "未记录具体原因（旧版或未知错误）"

    def summary(self):
        if not self.path.exists():
            return {
                "days": 7,
                "calls": 0,
                "success_rate": None,
                "operations": [],
                "coverage": "仅接入后的本系统API与已埋点MCP工具，不代表其他客户端Skill使用率",
            }
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT channel,operation,
                SUM(calls),SUM(CASE WHEN outcome='success' THEN calls ELSE 0 END),
                SUM(CASE WHEN outcome='warning' THEN calls ELSE 0 END),SUM(duration_ms)
                FROM usage WHERE hour>=? GROUP BY channel,operation""",
                ((datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H"),),
            ).fetchall()
            failure_rows = connection.execute(
                """SELECT channel,operation,reason,SUM(calls),MAX(last_seen_at)
                FROM usage_failures WHERE hour>=?
                GROUP BY channel,operation,reason
                ORDER BY SUM(calls) DESC,MAX(last_seen_at) DESC
                LIMIT 20""",
                ((datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H"),),
            ).fetchall()
        operations = [
            {
                "channel": r[0],
                "operation": r[1],
                "calls": r[2],
                "successes": r[3],
                "warnings": r[4],
                "failures": r[2] - r[3] - r[4],
                "average_ms": round(r[5] / r[2], 1),
            }
            for r in rows
        ]
        total = sum(r[2] for r in rows)
        failure_count = sum(item["failures"] for item in operations)
        return {
            "days": 7,
            "calls": total,
            "success_rate": sum(r[3] for r in rows) / total if total else None,
            "operations": operations,
            "failure_count": failure_count,
            "failure_details": [
                {
                    "channel": row[0],
                    "operation": row[1],
                    "reason": row[2],
                    "calls": row[3],
                    "last_seen_at": row[4],
                }
                for row in failure_rows
            ],
            "failure_detail_coverage": (
                "当前版本开始记录失败原因；旧的聚合失败次数无法追溯具体原因。"
                if failure_count and not failure_rows
                else "失败原因只保存固定类别，不保存请求内容、路径或异常原文。"
            ),
            "coverage": "仅本系统API与已埋点MCP工具的技术调用；非准确率。外部Skill暂不可观测。",
        }


def observe_tool(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        started = time.monotonic()
        outcome = "error"
        reason = "工具执行异常"
        try:
            result = function(*args, **kwargs)
            outcome = result.get("status", "success") if isinstance(result, dict) else "success"
            if outcome not in {"success", "warning"}:
                outcome = "error"
                reason = "工具返回失败状态"
            else:
                reason = None
            return result
        finally:
            UsageMetrics(function.__globals__["system"]().settings.data_root).record(
                "mcp", function.__name__, outcome, time.monotonic() - started, reason
            )

    return wrapped
