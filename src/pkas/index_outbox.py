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
            "deferred": counts.get("deferred", 0),
            "failed": counts.get("failed", 0),
        }

    def _resolve_reconciled_events(self, before: str) -> dict[str, int]:
        """Close old events after a successful full vector reconciliation.

        The outbox is a durable change log, not a second vector index.  Older
        events can remain after a rebuild or a change of embedding scope.  Once
        ``vector_index.sync`` has reconciled the current eligible set, those
        events must not remain visible as actionable work.  Upserts outside the
        configured embedding scope are retained as ``deferred`` evidence of the
        policy; the rest are completed.  The timestamp fence keeps changes
        committed during the reconciliation available for the next cycle.
        """
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            params = [before]
            settings = getattr(self.vector_index, "settings", None)
            embedding_scope = getattr(settings, "embedding_scope", "all_formal")
            allow_restricted = bool(
                getattr(settings, "embedding_allow_restricted_remote_processing", False)
            )
            scope_clause = ""
            if embedding_scope == "selected_l3":
                scope_clause = (
                    " AND json_extract(s.metadata_json, "
                    "'$.requested_processing_level') = 'L3'"
                )
            restricted_clause = ""
            if not allow_restricted:
                restricted_clause = " AND c.privacy <> 'restricted'"

            cursor = connection.execute(
                f"""UPDATE index_outbox
                    SET status='deferred', last_error_code='outside_embedding_scope',
                        updated_at=?
                    WHERE status='pending' AND operation='upsert'
                      AND entity_type='chunk' AND updated_at <= ?
                      AND entity_id NOT IN (
                          SELECT c.id FROM chunks c
                          JOIN sources s ON s.id=c.source_id
                          WHERE s.status='indexed'
                            AND s.source_type NOT IN
                                ('codex-turn','thread-summary','thread-journal')
                            {scope_clause}{restricted_clause}
                      )""",
                (now, *params),
            )
            deferred = int(cursor.rowcount)
            cursor = connection.execute(
                """UPDATE index_outbox
                   SET status='completed', last_error_code=NULL, updated_at=?
                   WHERE status='pending' AND updated_at <= ?""",
                (now, before),
            )
            completed = int(cursor.rowcount)
            connection.commit()
        return {"completed": completed, "deferred": deferred}

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
        reconciliation_started = utc_now()
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
            # Keep the durable queue bounded.  The previous implementation
            # claimed a small batch but asked the vector index to process every
            # eligible chunk, which made a large backlog look stuck and could
            # consume an unbounded embedding budget in one run.
            result = self.vector_index.sync(max_chunks=max(1, min(limit, 5000)))
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
        fully_reconciled = (
            result.get("status") == "completed" and int(result.get("pending") or 0) == 0
        )
        if result.get("status") == "completed":
            self._finish_claimed([int(row["id"]) for row in rows])
            resolved = (
                self._resolve_reconciled_events(reconciliation_started)
                if fully_reconciled
                else {"completed": 0, "deferred": 0}
            )
            status = "completed"
        else:
            self._retry_claimed(rows, "vector_sync_incomplete")
            resolved = {"completed": 0, "deferred": 0}
            status = "warning"
        return {
            "status": status,
            "claimed": len(rows),
            "completed": len(rows) if status == "completed" else 0,
            "requeued_stale": requeued,
            "vector": result,
            "coverage": coverage,
            "reconciled": fully_reconciled,
            "resolved_events": resolved,
            "stats": self.stats(),
        }
