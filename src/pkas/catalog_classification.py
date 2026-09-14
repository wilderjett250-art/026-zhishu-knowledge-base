"""Durable bridge from the machine A catalog to reviewed classification batches.

The A catalog remains a read-only file inventory. This ledger only stores the
small, derived inspection record for files a user explicitly placed in a batch.
It deliberately does not copy originals or turn a path index into knowledge.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CatalogClassificationLedger:
    """Append-only derived ledger for A-catalog classification work."""

    def __init__(self, data_root: Path):
        self.home = data_root / "catalog-classification"
        self.path = self.home / "ledger.sqlite"
        self.catalog_path = data_root / "machine-catalog" / "catalog.sqlite"
        self.home.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries(
                    catalog_file_id INTEGER PRIMARY KEY,
                    source_path TEXT NOT NULL UNIQUE,
                    scope_path TEXT NOT NULL,
                    catalog_category TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    source_job TEXT,
                    record TEXT,
                    summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS entries_state ON entries(state,catalog_file_id);
                CREATE INDEX IF NOT EXISTS entries_job ON entries(source_job);
                CREATE TABLE IF NOT EXISTS history(
                    id INTEGER PRIMARY KEY,
                    catalog_file_id INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    record TEXT,
                    summary TEXT,
                    source_job TEXT,
                    archived_at TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS history_file ON history(catalog_file_id,archived_at);
                """
            )

    def _attach_catalog(self, db: sqlite3.Connection) -> None:
        if not self.catalog_path.is_file():
            raise ValueError("A库索引尚未建立，无法创建分类批次")
        # This service only issues SELECT statements against the attached A catalog.
        # SQLite's connection-level query_only pragma would also block ledger writes.
        db.execute("ATTACH DATABASE ? AS catalog", (str(self.catalog_path),))

    @staticmethod
    def _detach_catalog(db: sqlite3.Connection) -> None:
        with suppress(sqlite3.DatabaseError):
            db.execute("DETACH DATABASE catalog")

    def progress(self) -> dict[str, int]:
        with self.connect() as db:
            self._attach_catalog(db)
            try:
                active_sql = "SELECT COUNT(*) FROM catalog.files WHERE state='active'"
                active = int(db.execute(active_sql).fetchone()[0])
                rows = dict(
                    db.execute("SELECT state,COUNT(*) FROM entries GROUP BY state").fetchall()
                )
            finally:
                self._detach_catalog(db)
        return {
            "catalog_active": active,
            "queued": int(rows.get("queued", 0)),
            "inspected": int(rows.get("inspected", 0)),
            "warning": int(rows.get("warning", 0)),
            "total_recorded": int(sum(rows.values())),
        }

    def reserve(self, job: str, limit: int) -> list[dict[str, Any]]:
        """Reserve only unseen or changed A-catalog files for one explicit job."""
        if limit < 1 or limit > 500:
            raise ValueError("每次分类批次应在1到500个文件之间")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._attach_catalog(db)
            try:
                rows = db.execute(
                    """
                    SELECT f.id,f.path,f.scope_path,f.category,f.byte_size,f.modified_ns
                    FROM catalog.files AS f
                    LEFT JOIN entries AS e ON e.catalog_file_id=f.id
                    WHERE f.state='active' AND (
                        e.catalog_file_id IS NULL OR
                        e.byte_size<>f.byte_size OR e.modified_ns<>f.modified_ns
                    )
                    ORDER BY f.id LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                timestamp = _now()
                result = []
                for row in rows:
                    previous = db.execute(
                        "SELECT * FROM entries WHERE catalog_file_id=?", (row["id"],)
                    ).fetchone()
                    if previous:
                        db.execute(
                            """INSERT INTO history(catalog_file_id,source_path,byte_size,
                               modified_ns,
                               state,record,summary,source_job,archived_at,reason)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (
                                previous["catalog_file_id"], previous["source_path"],
                                previous["byte_size"], previous["modified_ns"], previous["state"],
                                previous["record"], previous["summary"], previous["source_job"],
                                timestamp, "source_version_changed",
                            ),
                        )
                    db.execute(
                        """INSERT INTO entries(catalog_file_id,source_path,scope_path,
                           catalog_category,
                           byte_size,modified_ns,state,source_job,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(catalog_file_id) DO UPDATE SET
                           source_path=excluded.source_path,scope_path=excluded.scope_path,
                           catalog_category=excluded.catalog_category,byte_size=excluded.byte_size,
                           modified_ns=excluded.modified_ns,state=excluded.state,
                           source_job=excluded.source_job,record=NULL,summary=NULL,updated_at=excluded.updated_at""",
                        (
                            row["id"], row["path"], row["scope_path"], row["category"],
                            row["byte_size"], row["modified_ns"], "queued", job,
                            timestamp, timestamp,
                        ),
                    )
                    result.append(dict(row))
            finally:
                self._detach_catalog(db)
        return result

    def record_job(self, job: str, queue_path: Path) -> dict[str, int]:
        """Persist a completed job's derived results; originals remain at source_path."""
        queue = sqlite3.connect(queue_path)
        queue.row_factory = sqlite3.Row
        try:
            rows = queue.execute(
                "SELECT catalog_file_id,state,record,summary FROM files "
                "WHERE catalog_file_id IS NOT NULL ORDER BY id"
            ).fetchall()
        finally:
            queue.close()
        updated = warnings = 0
        with self.connect() as db:
            for row in rows:
                state = "inspected" if row["state"] == "inspected" else "warning"
                warnings += state == "warning"
                cursor = db.execute(
                    "UPDATE entries SET state=?,record=?,summary=?,updated_at=? "
                    "WHERE catalog_file_id=? AND source_job=?",
                    (state, row["record"], row["summary"], _now(), row["catalog_file_id"], job),
                )
                updated += cursor.rowcount
        return {"updated": updated, "warning": warnings}

    def classification_counts(self, taxonomy: dict[str, Any]) -> list[dict[str, Any]]:
        labels = {
            item["id"]: item["name"]
            for item in taxonomy["categories"]
            if item.get("parent_id")
        }
        totals: dict[str, dict[str, Any]] = {}
        with self.connect() as db:
            sql = (
                "SELECT record,byte_size FROM entries "
                "WHERE state='inspected' AND record IS NOT NULL"
            )
            rows = db.execute(sql).fetchall()
        for row in rows:
            record = json.loads(row["record"])
            category_id = record.get("classification", {}).get("category_id", "unresolved_other")
            current = totals.setdefault(
                category_id,
                {
                    "id": category_id,
                    "name": labels.get(category_id, category_id),
                    "count": 0,
                    "bytes": 0,
                },
            )
            current["count"] += 1
            current["bytes"] += int(row["byte_size"])
        return sorted(totals.values(), key=lambda item: (-item["count"], item["name"]))

    def selection(self, category_ids: list[str], mode: str) -> dict[str, Any]:
        selected = set(category_ids)
        items: list[dict[str, Any]] = []
        roots: set[str] = set()
        with self.connect() as db:
            rows = db.execute(
                """SELECT catalog_file_id,source_path,scope_path,byte_size,modified_ns,record
                   FROM entries WHERE state='inspected' AND record IS NOT NULL
                   ORDER BY catalog_file_id"""
            ).fetchall()
        for row in rows:
            record = json.loads(row["record"])
            classification = record.get("classification", {})
            if classification.get("category_id") not in selected:
                continue
            roots.add(row["scope_path"])
            items.append(
                {
                    "summary_file_id": f"catalog:{row['catalog_file_id']}",
                    "path": row["source_path"],
                    "relative": record.get("relative") or row["source_path"],
                    "bytes": int(row["byte_size"]),
                    "mtime_ns": int(row["modified_ns"]),
                    "content_category_id": classification.get("category_id"),
                    "content_category_label": classification.get("label"),
                    "classification_basis": classification.get("basis", "分类台账"),
                    "classification_revision": 1,
                }
            )
        if not items:
            raise ValueError("所选分类中没有已完成检查的文件")
        return {
            "source_summary_job": "catalog-classification-ledger",
            "root": "A库分类台账",
            "roots": sorted(roots),
            "mode": mode,
            "category_ids": category_ids,
            "items": items,
        }
