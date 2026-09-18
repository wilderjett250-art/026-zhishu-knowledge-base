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
from math import ceil
from pathlib import Path
from typing import Any

ALL_INSPECTED_SELECTION_ID = "__all_inspected__"
PROCESSING_MODES = frozenset({"catalog", "extract", "full", "semantic"})
READABLE_SUFFIXES = frozenset(
    {
        ".md", ".txt", ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".ppt",
        ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".xml", ".sql",
        ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".java", ".kt", ".c",
        ".cpp", ".h", ".cs", ".go", ".rs",
    }
)
ARCHIVE_OR_INSTALLER_SUFFIXES = frozenset(
    {".zip", ".7z", ".rar", ".tar", ".gz", ".exe", ".msi", ".apk"}
)
MEDIA_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3"})
# These files remain searchable through the A catalog.  Automatic whole-machine
# understanding skips them because they are usually transient runtime output;
# users can still explicitly inspect them through a manual catalog batch.
LOW_VALUE_AUTO_SUFFIXES = frozenset({".cache", ".err", ".lock", ".log", ".tmp"})
AI_PROTOCOL = "ai_understanding_v2"


def trusted_recommended_mode(record: dict[str, Any]) -> str | None:
    """Return an actionable AI recommendation only from the current audited protocol.

    A local category is useful for browsing, but it is not enough to decide how
    deeply a source should enter the searchable corpus.  The mixed-mode path
    must therefore never silently use an older/local/default field as an AI
    recommendation.
    """
    understanding = record.get("understanding")
    if not isinstance(understanding, dict):
        return None
    if (
        understanding.get("origin") != "agent"
        or understanding.get("protocol") != "ai_understanding_v2"
    ):
        return None
    mode = understanding.get("recommended_mode")
    return mode if mode in PROCESSING_MODES else None


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
                    ai_state TEXT NOT NULL DEFAULT 'pending',
                    ai_protocol TEXT,
                    ai_updated_at TEXT,
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
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(entries)").fetchall()
            }
            migrations = {
                "ai_state": (
                    "ALTER TABLE entries ADD COLUMN ai_state TEXT NOT NULL DEFAULT 'pending'"
                ),
                "ai_protocol": "ALTER TABLE entries ADD COLUMN ai_protocol TEXT",
                "ai_updated_at": "ALTER TABLE entries ADD COLUMN ai_updated_at TEXT",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    db.execute(statement)
            db.execute("CREATE INDEX IF NOT EXISTS entries_ai ON entries(ai_state,catalog_file_id)")
            # Older ledgers only had the local inspection state.  Backfill the
            # new explicit AI state without trusting a free-form old field.
            for row in db.execute(
                "SELECT catalog_file_id,state,record,ai_state FROM entries"
            ).fetchall():
                record = {}
                with suppress(ValueError, TypeError):
                    record = json.loads(row[2]) if row[2] else {}
                understanding = record.get("understanding")
                if (
                    isinstance(understanding, dict)
                    and understanding.get("origin") == "agent"
                    and understanding.get("protocol") == AI_PROTOCOL
                ):
                    ai_state, protocol = "done", AI_PROTOCOL
                elif row[1] in {"missing", "warning", "error"}:
                    ai_state, protocol = "failed", None
                else:
                    ai_state, protocol = "pending", None
                db.execute(
                    "UPDATE entries SET ai_state=?,ai_protocol=?,ai_updated_at=? "
                    "WHERE catalog_file_id=?",
                    (ai_state, protocol, _now() if ai_state != "pending" else None, row[0]),
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

    def progress(self) -> dict[str, Any]:
        with self.connect() as db:
            self._attach_catalog(db)
            try:
                active_sql = "SELECT COUNT(*) FROM catalog.files WHERE state='active'"
                active = int(db.execute(active_sql).fetchone()[0])
                rows = dict(
                    db.execute("SELECT state,COUNT(*) FROM entries GROUP BY state").fetchall()
                )
                excluded = ",".join("?" for _ in LOW_VALUE_AUTO_SUFFIXES)
                local_skipped = int(
                    db.execute(
                        f"SELECT COUNT(*) FROM catalog.files WHERE state='active' "
                        f"AND extension IN ({excluded})",
                        tuple(sorted(LOW_VALUE_AUTO_SUFFIXES)),
                    ).fetchone()[0]
                )
                coverage = db.execute(
                    f"""
                    WITH current AS (
                        SELECT e.ai_state
                        FROM entries AS e
                        INNER JOIN catalog.files AS f ON f.id=e.catalog_file_id
                        WHERE f.state='active'
                          AND f.extension NOT IN ({excluded})
                          AND e.byte_size=f.byte_size
                          AND e.modified_ns=f.modified_ns
                          AND e.ai_state IN ('done','failed')
                    )
                    SELECT
                        COALESCE(SUM(CASE WHEN ai_state='done' THEN 1 ELSE 0 END),0)
                            AS ai_done,
                        COALESCE(SUM(CASE WHEN ai_state='failed' THEN 1 ELSE 0 END),0)
                            AS ai_failed
                    FROM current
                    """,
                    tuple(sorted(LOW_VALUE_AUTO_SUFFIXES)),
                ).fetchone()
            finally:
                self._detach_catalog(db)
        ai_eligible = max(active - local_skipped, 0)
        ai_done = int(coverage["ai_done"] or 0)
        ai_failed = int(coverage["ai_failed"] or 0)
        ai_pending = max(ai_eligible - ai_done - ai_failed, 0)
        decision_done = ai_done + local_skipped
        return {
            "catalog_active": active,
            "queued": int(rows.get("queued", 0)),
            "inspected": int(rows.get("inspected", 0)),
            "warning": int(rows.get("warning", 0)),
            "total_recorded": int(sum(rows.values())),
            "ai_eligible": ai_eligible,
            "ai_done": ai_done,
            "ai_failed": ai_failed,
            "ai_pending": ai_pending,
            "local_skipped": local_skipped,
            "decision_done": decision_done,
            "decision_pending": max(active - decision_done, 0),
            "ai_coverage_percent": round(ai_done / ai_eligible * 100, 2)
            if ai_eligible
            else 100.0,
            "decision_coverage_percent": round(decision_done / active * 100, 2)
            if active
            else 100.0,
            "coverage_policy": (
                "AI速判所有非临时文件；.cache/.err/.lock/.log/.tmp按本地规则"
                "明确跳过；失败项单独保留"
            ),
        }

    @staticmethod
    def _reservation_priority(row: sqlite3.Row) -> tuple[int, int, int, int]:
        """Rank a bounded candidate window without forcing a catalog-wide SQL sort."""
        suffix = Path(str(row["path"])).suffix.casefold()
        if suffix in READABLE_SUFFIXES:
            tier = 0
        elif str(row["category"]).casefold() in {"document", "code", "data"}:
            tier = 1
        elif suffix in MEDIA_SUFFIXES:
            tier = 3
        elif suffix in ARCHIVE_OR_INSTALLER_SUFFIXES:
            tier = 4
        else:
            tier = 2
        byte_size = int(row["byte_size"])
        usable_size = 0 if 64 <= byte_size <= 20_000_000 else 1
        return tier, usable_size, -int(row["modified_ns"]), int(row["id"])

    def reserve(
        self,
        job: str,
        limit: int,
        *,
        directory_units: list[dict[str, str]] | None = None,
        automatic_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Reserve unseen/changed files, optionally from exact A-catalog directories.

        Automatic whole-machine batches keep only known transient runtime files
        in the location catalog. Every other file type receives a bounded AI
        triage opportunity, including binary/media/archive files; unreadable
        content is still a model-visible metadata-only decision.
        """
        if limit < 1 or limit > 500:
            raise ValueError("每次分类批次应在1到500个文件之间")
        pairs: list[tuple[str, str]] = []
        for unit in directory_units or []:
            scope_path, top_group = unit.get("scope_path"), unit.get("top_group")
            if not isinstance(scope_path, str) or not isinstance(top_group, str):
                raise ValueError("A库目录范围无效")
            pair = (scope_path, top_group)
            if pair not in pairs:
                pairs.append(pair)
        if len(pairs) > 400:
            raise ValueError("本次自动选择目录过多，请按批创建文件检查队列")
        candidate_window = min(max(limit * 20, 400), 5_000)
        base_sql = """
            SELECT f.id,f.path,f.scope_path,f.top_group,f.category,f.byte_size,f.modified_ns
            FROM catalog.files AS f
            LEFT JOIN entries AS e ON e.catalog_file_id=f.id
            WHERE f.state='active' AND (
                e.catalog_file_id IS NULL OR
                e.byte_size<>f.byte_size OR e.modified_ns<>f.modified_ns
            )
        """
        base_params: tuple[str, ...] = ()
        if automatic_only:
            excluded_placeholders = ",".join("?" for _ in LOW_VALUE_AUTO_SUFFIXES)
            base_sql += f" AND LOWER(f.extension) NOT IN ({excluded_placeholders})"
            base_params = tuple(sorted(LOW_VALUE_AUTO_SUFFIXES))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._attach_catalog(db)
            try:
                if pairs:
                    # idx_catalog_group(scope_path, top_group, state) keeps this
                    # bounded even when the selected plan spans hundreds of folders.
                    per_directory = min(50, max(4, ceil(candidate_window / len(pairs))))
                    rows = []
                    for scope_path, top_group in pairs:
                        rows.extend(
                            db.execute(
                                base_sql
                                + " AND f.scope_path=? AND f.top_group=? ORDER BY f.id LIMIT ?",
                                (*base_params, scope_path, top_group, per_directory),
                            ).fetchall()
                        )
                else:
                    rows = db.execute(
                        base_sql + " ORDER BY f.id LIMIT ?", (*base_params, candidate_window)
                    ).fetchall()
                rows = sorted(rows, key=self._reservation_priority)[:limit]
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
                           byte_size,modified_ns,state,source_job,record,summary,
                           ai_state,ai_protocol,ai_updated_at,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(catalog_file_id) DO UPDATE SET
                           source_path=excluded.source_path,scope_path=excluded.scope_path,
                           catalog_category=excluded.catalog_category,byte_size=excluded.byte_size,
                           modified_ns=excluded.modified_ns,state=excluded.state,
                           source_job=excluded.source_job,record=NULL,summary=NULL,
                           ai_state='pending',ai_protocol=NULL,ai_updated_at=NULL,
                           updated_at=excluded.updated_at""",
                        (
                            row["id"], row["path"], row["scope_path"], row["category"],
                            row["byte_size"], row["modified_ns"], "queued", job,
                            None, None, "pending", None, None, timestamp, timestamp,
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
                "SELECT catalog_file_id,state,record,summary,ai_state FROM files "
                "WHERE catalog_file_id IS NOT NULL ORDER BY id"
            ).fetchall()
        finally:
            queue.close()
        updated = warnings = 0
        with self.connect() as db:
            for row in rows:
                state = (
                    "inspected"
                    if row["state"] == "inspected"
                    else "missing"
                    if row["state"] == "missing"
                    else "warning"
                )
                warnings += state == "warning"
                record = {}
                with suppress(ValueError, TypeError):
                    record = json.loads(row["record"]) if row["record"] else {}
                understanding = record.get("understanding")
                agent_done = (
                    isinstance(understanding, dict)
                    and understanding.get("origin") == "agent"
                    and understanding.get("protocol") == AI_PROTOCOL
                )
                ai_state = "done" if agent_done else "failed" if state in {
                    "missing", "warning"
                } else "pending"
                ai_protocol = AI_PROTOCOL if agent_done else None
                cursor = db.execute(
                    "UPDATE entries SET state=?,record=?,summary=?,ai_state=?,"
                    "ai_protocol=?,ai_updated_at=?,updated_at=? "
                    "WHERE catalog_file_id=? AND source_job=?",
                    (
                        state, row["record"], row["summary"], ai_state, ai_protocol,
                        _now() if ai_state != "pending" else None, _now(),
                        row["catalog_file_id"], job,
                    ),
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
                    "recommendations": {
                        "catalog": 0,
                        "extract": 0,
                        "full": 0,
                        "semantic": 0,
                        "unavailable": 0,
                    },
                },
            )
            current["count"] += 1
            current["bytes"] += int(row["byte_size"])
            recommendation = trusted_recommended_mode(record)
            current["recommendations"][recommendation or "unavailable"] += 1
        result = sorted(totals.values(), key=lambda item: (-item["count"], item["name"]))
        if result:
            recommendations = {
                mode: sum(item["recommendations"][mode] for item in result)
                for mode in ("catalog", "extract", "full", "semantic", "unavailable")
            }
            result.append(
                {
                    "id": ALL_INSPECTED_SELECTION_ID,
                    "name": "全部已检查资料",
                    "count": sum(item["count"] for item in result),
                    "bytes": sum(item["bytes"] for item in result),
                    "recommendations": recommendations,
                    "system": True,
                }
            )
        return result

    def selection(
        self, category_ids: list[str], mode: str, *, include_all_inspected: bool = False
    ) -> dict[str, Any]:
        selected = set(category_ids)
        items: list[dict[str, Any]] = []
        roots: set[str] = set()
        recommendation_unavailable = 0
        with self.connect() as db:
            rows = db.execute(
                """SELECT catalog_file_id,source_path,scope_path,byte_size,modified_ns,record
                   FROM entries WHERE state='inspected' AND record IS NOT NULL
                   ORDER BY catalog_file_id"""
            ).fetchall()
        for row in rows:
            record = json.loads(row["record"])
            classification = record.get("classification", {})
            matched_categories = {
                classification.get("category_id"),
                *classification.get("secondary_category_ids", []),
            }
            if not include_all_inspected and not (matched_categories & selected):
                continue
            recommendation = trusted_recommended_mode(record)
            if mode == "recommended" and recommendation is None:
                recommendation_unavailable += 1
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
                    "ai_understanding": record.get("understanding"),
                    "recommended_action": recommendation,
                }
            )
        if not items:
            if mode == "recommended":
                raise ValueError("所选范围没有可核验的AI处理建议，请先完成Luna理解")
            raise ValueError("所选范围中没有已完成检查的文件")
        return {
            "source_summary_job": "catalog-classification-ledger",
            "root": "A库分类台账",
            "roots": sorted(roots),
            "mode": mode,
            "category_ids": category_ids,
            "include_all_inspected": include_all_inspected,
            "recommendation_unavailable": recommendation_unavailable,
            "items": items,
        }

    def selection_file_ids(
        self, catalog_file_ids: list[int], mode: str, *, source: str
    ) -> dict[str, Any]:
        """Build a narrow intake selection from already reviewed catalogue identities."""
        requested = set(catalog_file_ids)
        if not requested:
            raise ValueError("请选择至少一个项目")
        items: list[dict[str, Any]] = []
        roots: set[str] = set()
        with self.connect() as db:
            placeholders = ",".join("?" for _ in requested)
            rows = db.execute(
                f"""SELECT catalog_file_id,source_path,scope_path,byte_size,modified_ns,record
                    FROM entries WHERE state='inspected' AND record IS NOT NULL
                    AND catalog_file_id IN ({placeholders}) ORDER BY catalog_file_id""",
                tuple(sorted(requested)),
            ).fetchall()
        for row in rows:
            record = json.loads(row["record"])
            recommendation = trusted_recommended_mode(record)
            if mode == "recommended" and recommendation is None:
                continue
            classification = record.get("classification", {})
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
                    "ai_understanding": record.get("understanding"),
                    "recommended_action": recommendation,
                }
            )
        if not items:
            raise ValueError("所选项目没有可复用的已验证资料")
        return {
            "source_summary_job": source,
            "root": "项目总览候选",
            "roots": sorted(roots),
            "mode": mode,
            "category_ids": sorted(
                {item["content_category_id"] for item in items if item["content_category_id"]}
            ),
            "items": items,
        }
