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
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, channel: str, operation: str, outcome: str, elapsed: float):
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO usage VALUES(?,?,?,?,1,?)
                    ON CONFLICT(hour,channel,operation,outcome) DO UPDATE SET
                    calls=calls+1,duration_ms=duration_ms+excluded.duration_ms""",
                    (
                        datetime.now(UTC).strftime("%Y-%m-%dT%H"),
                        channel,
                        operation,
                        outcome,
                        max(0, elapsed * 1000),
                    ),
                )
                connection.execute(
                    "DELETE FROM usage WHERE hour < ?",
                    ((datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H"),),
                )
        except (sqlite3.Error, OSError):
            logging.getLogger(__name__).warning("Usage aggregate write unavailable")

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
        return {
            "days": 7,
            "calls": total,
            "success_rate": sum(r[3] for r in rows) / total if total else None,
            "operations": operations,
            "coverage": "仅本系统API与已埋点MCP工具的技术调用；非准确率。外部Skill暂不可观测。",
        }


def observe_tool(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        started = time.monotonic()
        outcome = "error"
        try:
            result = function(*args, **kwargs)
            outcome = result.get("status", "success") if isinstance(result, dict) else "success"
            if outcome not in {"success", "warning"}:
                outcome = "error"
            return result
        finally:
            UsageMetrics(function.__globals__["system"]().settings.data_root).record(
                "mcp", function.__name__, outcome, time.monotonic() - started
            )

    return wrapped
