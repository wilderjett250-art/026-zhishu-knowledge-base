from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pkas.db import Database
from pkas.local_lock import WindowsFileLock
from pkas.repository import utc_now
from pkas.vector_index import QdrantVectorIndex, VectorIndexError


class IndexOutbox:
    """Durable bridge between committed SQLite facts and rebuildable vector state."""

    def __init__(self, database: Database, vector_index: QdrantVectorIndex) -> None:
        self.database = database
        self.vector_index = vector_index

    def stats(self) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM index_outbox GROUP BY status"
            ).fetchall()
        counts = {row["status"]: int(row["n"]) for row in rows}
        return {
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
        }

    def requeue_stale(self, stale_minutes: int = 15) -> int:
        cutoff = (datetime.now(UTC) - timedelta(minutes=stale_minutes)).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """UPDATE index_outbox SET status='pending', available_at=?, updated_at=?
                WHERE status='processing' AND updated_at < ?""",
                (utc_now(), utc_now(), cutoff),
            )
            connection.commit()
            return int(cursor.rowcount)

    def claim(self, limit: int = 500) -> list[dict[str, Any]]:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM index_outbox
                WHERE status='pending' AND available_at<=?
                ORDER BY id LIMIT ?""",
                (now, max(1, min(limit, 5000))),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"""UPDATE index_outbox
                    SET status='processing', attempts=attempts+1, updated_at=?
                    WHERE id IN ({placeholders})""",
                    (now, *ids),
                )
            connection.commit()
        return [dict(row) for row in rows]

    def _finish_claimed(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.database.connect() as connection:
            connection.execute(
                f"""UPDATE index_outbox SET status='completed', last_error_code=NULL,
                updated_at=? WHERE id IN ({placeholders})""",
                (utc_now(), *ids),
            )
            connection.commit()

    def _retry_claimed(self, rows: list[dict[str, Any]], error_code: str) -> None:
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            for row in rows:
                attempts = int(row["attempts"]) + 1
                terminal = attempts >= 5
                delay = min(3600, 30 * (2 ** max(0, attempts - 1)))
                connection.execute(
                    """UPDATE index_outbox SET status=?, available_at=?,
                    last_error_code=?, updated_at=? WHERE id=?""",
                    (
                        "failed" if terminal else "pending",
                        (now + timedelta(seconds=delay)).isoformat(),
                        error_code,
                        now.isoformat(),
                        row["id"],
                    ),
                )
            connection.commit()

    def process(self, limit: int = 500) -> dict[str, Any]:
        if not self.vector_index.enabled:
            return {
                "status": "completed",
                "claimed": 0,
                "completed": 0,
                "deferred": "embedding_disabled",
                "stats": self.stats(),
            }
        lock = WindowsFileLock(
            self.database.settings.data_root / "runtime" / "vector-index.lock"
        )
        if not lock.acquire():
            return {
                "status": "completed",
                "claimed": 0,
                "completed": 0,
                "deferred": "another_vector_worker",
                "stats": self.stats(),
            }
        try:
            return self._process_locked(limit)
        finally:
            lock.release()

    def _process_locked(self, limit: int) -> dict[str, Any]:
        requeued = self.requeue_stale()
        rows = self.claim(limit)
        if not rows:
            return {
                "status": "completed",
                "claimed": 0,
                "completed": 0,
                "requeued_stale": requeued,
                "stats": self.stats(),
            }
        try:
            result = self.vector_index.sync(max_chunks=None)
        except VectorIndexError as exc:
            self._retry_claimed(rows, type(exc).__name__)
            return {
                "status": "warning",
                "claimed": len(rows),
                "completed": 0,
                "requeued_stale": requeued,
                "error_code": type(exc).__name__,
                "stats": self.stats(),
            }
        coverage = self.vector_index.coverage()
        if result.get("status") == "completed" and int(coverage["pending"]) == 0:
            self._finish_claimed([int(row["id"]) for row in rows])
            status = "completed"
        else:
            self._retry_claimed(rows, "vector_sync_incomplete")
            status = "warning"
        return {
            "status": status,
            "claimed": len(rows),
            "completed": len(rows) if status == "completed" else 0,
            "requeued_stale": requeued,
            "vector": result,
            "coverage": coverage,
            "stats": self.stats(),
        }
