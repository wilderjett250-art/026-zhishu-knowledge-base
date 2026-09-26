"""Durable bridge from the machine A catalog to reviewed classification batches.

The A catalog remains a read-only file inventory. This ledger only stores the
small, derived inspection record for files a user explicitly placed in a batch.
It deliberately does not copy originals or turn a path index into knowledge.
"""

from __future__ import annotations

import errno
import json
import shutil
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any

from pkas.catalog_scope import (
    AUTO_SUFFIXES,
    GENERATED_PARTS,
    IMAGE_SUFFIXES,
    candidate_priority,
    load_auto_policy,
    policy_signature,
)
from pkas.content_taxonomy import classify, load_taxonomy
from pkas.file_inspector import inspect_file
from pkas.ingest import is_sensitive_path
from pkas.intake import EXCLUDED, linked
from pkas.local_lock import WindowsFileLock

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
AI_PROTOCOL = "ai_understanding_v2"
PREFLIGHT_MIN_FREE_BYTES = 750_000_000


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
        self.data_root = data_root
        self.home = data_root / "catalog-classification"
        self.path = self.home / "ledger.sqlite"
        self.catalog_path = data_root / "machine-catalog" / "catalog.sqlite"
        self.home.mkdir(parents=True, exist_ok=True)
        self._scope_thread: threading.Thread | None = None
        self._scope_error: str | None = None
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, uri=True)
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
                CREATE TABLE IF NOT EXISTS auto_candidates(
                    catalog_file_id INTEGER PRIMARY KEY,
                    byte_size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    priority INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS auto_candidates_ready
                    ON auto_candidates(state,priority,catalog_file_id);
                CREATE TABLE IF NOT EXISTS auto_scope_meta(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    catalog_job_id TEXT NOT NULL,
                    policy_signature TEXT NOT NULL,
                    active_total INTEGER NOT NULL,
                    candidate_total INTEGER NOT NULL,
                    built_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_preflight(
                    catalog_file_id INTEGER PRIMARY KEY,
                    byte_size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    prefix_sha256 TEXT,
                    detected_type TEXT,
                    coverage TEXT NOT NULL,
                    category_id TEXT NOT NULL,
                    classification_basis TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    privacy_classification TEXT NOT NULL DEFAULT 'restricted',
                    review_status TEXT NOT NULL DEFAULT 'unreviewed',
                    taxonomy_revision TEXT NOT NULL DEFAULT '',
                    inspected_at TEXT NOT NULL
                );
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
            preflight_columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(local_preflight)").fetchall()
            }
            if "privacy_classification" not in preflight_columns:
                db.execute(
                    "ALTER TABLE local_preflight ADD COLUMN privacy_classification "
                    "TEXT NOT NULL DEFAULT 'restricted'"
                )
            if "review_status" not in preflight_columns:
                db.execute(
                    "ALTER TABLE local_preflight ADD COLUMN review_status "
                    "TEXT NOT NULL DEFAULT 'unreviewed'"
                )
            if "taxonomy_revision" not in preflight_columns:
                # Legacy suggestions have unknown provenance and must not be
                # silently shown as if produced by the current rule revision.
                db.execute(
                    "ALTER TABLE local_preflight ADD COLUMN taxonomy_revision "
                    "TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS local_preflight_category "
                "ON local_preflight(taxonomy_revision,category_id,catalog_file_id)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS entries_ai ON entries(ai_state,catalog_file_id)")
            # Older ledgers only had the local inspection state.  Backfill the
            # new explicit AI state without trusting a free-form old field.
            if "ai_state" in migrations and "ai_state" not in columns:
                for row in db.execute(
                    "SELECT catalog_file_id,state,record FROM entries"
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
        # Force the 10+ GB source inventory read-only even if a future query
        # accidentally tries to mutate it. query_only would also block ledger writes.
        db.execute(
            "ATTACH DATABASE ? AS catalog",
            (self.catalog_path.as_uri() + "?mode=ro",),
        )

    @staticmethod
    def _detach_catalog(db: sqlite3.Connection) -> None:
        with suppress(sqlite3.DatabaseError):
            db.execute("DETACH DATABASE catalog")

    def _scope_identity(self, db: sqlite3.Connection) -> tuple[str, str]:
        latest = db.execute(
            "SELECT id,state FROM catalog.jobs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not latest or latest["state"] != "completed":
            raise ValueError("A库目录扫描尚未完成，不能计算自动分级范围")
        return str(latest["id"]), policy_signature(load_auto_policy(self.data_root))

    def auto_scope_ready(self) -> bool:
        if not self.catalog_path.is_file():
            return False
        with self.connect() as db:
            self._attach_catalog(db)
            try:
                job_id, signature = self._scope_identity(db)
                row = db.execute(
                    "SELECT catalog_job_id,policy_signature FROM auto_scope_meta WHERE id=1"
                ).fetchone()
                return bool(
                    row and row["catalog_job_id"] == job_id
                    and row["policy_signature"] == signature
                )
            except ValueError:
                return False
            finally:
                self._detach_catalog(db)

    def refresh_auto_scope(self) -> dict[str, Any]:
        """Materialize a small candidate list; all other catalog paths are L0.

        The 10+ GB A catalog is read-only. A staging table keeps an earlier
        candidate list available if this long scan is interrupted.
        """
        with WindowsFileLock(self.home / "scope-build.lock"):
            policy = load_auto_policy(self.data_root)
            with self.connect() as db:
                self._attach_catalog(db)
                try:
                    job_id, signature = self._scope_identity(db)
                    previous = db.execute(
                        "SELECT * FROM auto_scope_meta WHERE id=1"
                    ).fetchone()
                    if previous and previous["catalog_job_id"] == job_id and previous[
                        "policy_signature"
                    ] == signature:
                        return dict(previous)

                    db.execute("DROP TABLE IF EXISTS auto_candidates_build")
                    db.execute(
                        """CREATE TABLE auto_candidates_build(
                            catalog_file_id INTEGER PRIMARY KEY,
                            byte_size INTEGER NOT NULL,
                            modified_ns INTEGER NOT NULL,
                            priority INTEGER NOT NULL,
                            state TEXT NOT NULL DEFAULT 'pending'
                        )"""
                    )
                    db.commit()
                    candidates = 0
                    for extension in sorted(AUTO_SUFFIXES):
                        pending: list[tuple[int, int, int, int]] = []
                        rows = db.execute(
                            "SELECT id,path,byte_size,modified_ns FROM catalog.files "
                            "WHERE state='active' AND extension=?",
                            (extension,),
                        )
                        for row in rows:
                            priority, _ = candidate_priority(
                                str(row["path"]), extension, policy, int(row["byte_size"])
                            )
                            if priority is None:
                                continue
                            pending.append(
                                (
                                    int(row["id"]), int(row["byte_size"]),
                                    int(row["modified_ns"]), priority,
                                )
                            )
                            if len(pending) >= 2000:
                                db.executemany(
                                    "INSERT INTO auto_candidates_build "
                                    "(catalog_file_id,byte_size,modified_ns,priority) "
                                    "VALUES(?,?,?,?)",
                                    pending,
                                )
                                candidates += len(pending)
                                pending.clear()
                                db.commit()
                        if pending:
                            db.executemany(
                                "INSERT INTO auto_candidates_build "
                                "(catalog_file_id,byte_size,modified_ns,priority) "
                                "VALUES(?,?,?,?)",
                                pending,
                            )
                            candidates += len(pending)
                            db.commit()

                    cached = {}
                    with suppress(OSError, ValueError, TypeError):
                        cached = json.loads(
                            (self.data_root / "machine-catalog" / "status.json").read_text(
                                encoding="utf-8"
                            )
                        )
                    active = (
                        int(cached["catalog_files"])
                        if cached.get("job_id") == job_id
                        else int(
                            db.execute(
                                "SELECT COUNT(*) FROM catalog.files WHERE state='active'"
                            ).fetchone()[0]
                        )
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='done'
                           WHERE EXISTS(SELECT 1 FROM entries e
                             WHERE e.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND e.byte_size=auto_candidates_build.byte_size
                               AND e.modified_ns=auto_candidates_build.modified_ns
                               AND e.ai_state='done' AND e.ai_protocol=?)""",
                        (AI_PROTOCOL,),
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='local_inspected'
                           WHERE state='pending' AND EXISTS(SELECT 1 FROM local_preflight p
                             WHERE p.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND p.byte_size=auto_candidates_build.byte_size
                               AND p.modified_ns=auto_candidates_build.modified_ns
                               AND p.outcome='sampled')"""
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='local_l0'
                           WHERE state='pending' AND EXISTS(SELECT 1 FROM local_preflight p
                             WHERE p.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND p.byte_size=auto_candidates_build.byte_size
                               AND p.modified_ns=auto_candidates_build.modified_ns
                               AND p.outcome='local_l0')"""
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='needs_review'
                           WHERE state='pending' AND EXISTS(SELECT 1 FROM local_preflight p
                             WHERE p.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND p.byte_size=auto_candidates_build.byte_size
                               AND p.modified_ns=auto_candidates_build.modified_ns
                               AND p.outcome='needs_review')"""
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='local_l0'
                           WHERE state='pending' AND EXISTS(SELECT 1 FROM entries e
                             WHERE e.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND e.byte_size=auto_candidates_build.byte_size
                               AND e.modified_ns=auto_candidates_build.modified_ns
                               AND e.ai_state='local_l0')"""
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='local_inspected'
                           WHERE state='pending' AND EXISTS(SELECT 1 FROM entries e
                             WHERE e.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND e.byte_size=auto_candidates_build.byte_size
                               AND e.modified_ns=auto_candidates_build.modified_ns
                               AND e.state='inspected' AND e.ai_state='pending')"""
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='reserved'
                           WHERE state='pending' AND EXISTS(SELECT 1 FROM entries e
                             WHERE e.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND e.byte_size=auto_candidates_build.byte_size
                               AND e.modified_ns=auto_candidates_build.modified_ns
                               AND e.state='queued')"""
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='failed'
                           WHERE state='pending' AND EXISTS(SELECT 1 FROM entries e
                             WHERE e.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND e.byte_size=auto_candidates_build.byte_size
                               AND e.modified_ns=auto_candidates_build.modified_ns
                               AND e.ai_state IN ('failed','rejected'))"""
                    )
                    db.execute(
                        """UPDATE auto_candidates_build SET state='needs_review'
                           WHERE state IN ('pending','local_inspected')
                             AND EXISTS(SELECT 1 FROM entries e
                             WHERE e.catalog_file_id=auto_candidates_build.catalog_file_id
                               AND e.byte_size=auto_candidates_build.byte_size
                               AND e.modified_ns=auto_candidates_build.modified_ns
                               AND e.ai_state='needs_review')"""
                    )
                    db.execute("DROP TABLE auto_candidates")
                    db.execute(
                        "ALTER TABLE auto_candidates_build RENAME TO auto_candidates"
                    )
                    db.execute(
                        "CREATE INDEX auto_candidates_ready "
                        "ON auto_candidates(state,priority,catalog_file_id)"
                    )
                    built_at = _now()
                    db.execute(
                        """INSERT OR REPLACE INTO auto_scope_meta
                           (id,catalog_job_id,policy_signature,active_total,
                            candidate_total,built_at) VALUES(1,?,?,?,?,?)""",
                        (job_id, signature, active, candidates, built_at),
                    )
                    db.commit()
                    self._scope_error = None
                    return {
                        "catalog_job_id": job_id,
                        "policy_signature": signature,
                        "active_total": active,
                        "candidate_total": candidates,
                        "built_at": built_at,
                    }
                finally:
                    self._detach_catalog(db)

    def schedule_auto_scope_refresh(self) -> None:
        if self._scope_error or self.auto_scope_ready() or (
            self._scope_thread is not None and self._scope_thread.is_alive()
        ):
            return
        with self.connect() as db:
            self._attach_catalog(db)
            try:
                self._scope_identity(db)
            except ValueError:
                # Wait for the catalog scan to finish before scheduling the gate.
                return
            finally:
                self._detach_catalog(db)

        def build() -> None:
            try:
                self.refresh_auto_scope()
            except RuntimeError:
                # Another process owns the builder lock; a later poll may retry.
                return
            except (OSError, sqlite3.Error, ValueError) as error:
                self._scope_error = type(error).__name__

        self._scope_thread = threading.Thread(target=build, daemon=True)
        self._scope_thread.start()

    def retry_auto_scope_refresh(self) -> None:
        self._scope_error = None
        self.schedule_auto_scope_refresh()

    def progress(self) -> dict[str, Any]:
        with self.connect() as db:
            self._attach_catalog(db)
            try:
                latest = db.execute(
                    "SELECT id FROM catalog.jobs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                latest_id = str(latest["id"]) if latest else ""
                scope = db.execute(
                    "SELECT * FROM auto_scope_meta WHERE id=1"
                ).fetchone()
                signature = policy_signature(load_auto_policy(self.data_root))
                fresh = bool(
                    scope and scope["catalog_job_id"] == latest_id
                    and scope["policy_signature"] == signature
                )
                rows = dict(
                    db.execute("SELECT state,COUNT(*) FROM entries GROUP BY state").fetchall()
                )
                candidate_states = dict(
                    db.execute(
                        "SELECT state,COUNT(*) FROM auto_candidates GROUP BY state"
                    ).fetchall()
                ) if fresh else {}
            finally:
                self._detach_catalog(db)
        cached = {}
        with suppress(OSError, ValueError, TypeError):
            cached = json.loads(
                (self.data_root / "machine-catalog" / "status.json").read_text(
                    encoding="utf-8"
                )
            )
        active = (
            int(scope["active_total"])
            if fresh and scope
            else int(cached.get("catalog_files", 0))
        )
        candidates = int(scope["candidate_total"]) if fresh and scope else 0
        local_candidate_l0 = int(candidate_states.get("local_l0", 0))
        local_skipped = max(0, active - candidates) + local_candidate_l0 if fresh else 0
        ai_eligible = max(0, candidates - local_candidate_l0)
        ai_done = int(candidate_states.get("done", 0))
        ai_failed = int(candidate_states.get("failed", 0))
        review_pending = int(candidate_states.get("needs_review", 0))
        ai_pending = max(0, ai_eligible - ai_done - ai_failed)
        local_read_done = sum(
            int(candidate_states.get(state, 0))
            for state in ("local_inspected", "local_l0", "done", "failed", "needs_review")
        )
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
            "review_pending": review_pending,
            "ai_ready": int(candidate_states.get("local_inspected", 0)),
            "local_read_done": local_read_done,
            "local_read_pending": max(0, candidates - local_read_done),
            "local_read_coverage_percent": round(local_read_done / candidates * 100, 2)
            if candidates else 0.0,
            "local_skipped": local_skipped,
            "ai_candidates_total": candidates,
            "local_candidate_l0": local_candidate_l0,
            "local_inspected": int(candidate_states.get("local_inspected", 0)),
            "scope_state": "ready" if fresh else "error" if self._scope_error else "building",
            "scope_error": self._scope_error if not fresh else None,
            "scope_built_at": str(scope["built_at"]) if fresh and scope else None,
            "decision_done": decision_done,
            "decision_pending": max(active - decision_done, 0),
            "ai_coverage_percent": round(ai_done / ai_eligible * 100, 2)
            if ai_eligible else 0.0,
            "decision_coverage_percent": round(decision_done / active * 100, 2)
            if active and fresh else 0.0,
            "coverage_policy": (
                "按当前四级方案：系统与依赖产物保留L0位置索引；"
                "文档候选逐文件轻检后由AI建议L0/L1/L2/L3；失败项单独复查"
            ),
        }

    def _safe_preflight_path(self, path: Path, scope: Path) -> bool:
        """Recheck the catalog boundary before reading a possibly changed path."""
        resolved = path.resolve(strict=False)
        root = scope.resolve(strict=False)
        data = self.data_root.resolve(strict=False)
        return bool(
            (resolved == root or root in resolved.parents)
            and data not in (resolved, *resolved.parents)
            and not linked(path)
            and not is_sensitive_path(path)
            and not any(
                part.casefold() in EXCLUDED | GENERATED_PARTS for part in path.parts
            )
        )

    def preflight_categories(self) -> dict[str, Any]:
        """Provisional, content-sampled categories; never an import selection."""
        taxonomy = load_taxonomy(self.data_root)
        names = {item["id"]: item["name"] for item in taxonomy["categories"]}
        parents = {
            item["id"]: item["parent_id"] for item in taxonomy["categories"]
        }
        with self.connect() as db:
            rows = db.execute(
                """SELECT p.category_id,COUNT(*) AS total
                   FROM local_preflight p
                   JOIN auto_candidates a ON a.catalog_file_id=p.catalog_file_id
                   WHERE p.outcome='sampled' AND a.state='local_inspected'
                     AND a.byte_size=p.byte_size
                     AND a.modified_ns=p.modified_ns
                     AND p.taxonomy_revision=?
                   GROUP BY p.category_id ORDER BY total DESC,p.category_id""",
                (taxonomy["revision"],),
            ).fetchall()
            stale_total = int(db.execute(
                """SELECT COUNT(*) FROM local_preflight p
                   JOIN auto_candidates a ON a.catalog_file_id=p.catalog_file_id
                   WHERE p.outcome='sampled' AND a.state='local_inspected'
                     AND a.byte_size=p.byte_size
                     AND a.modified_ns=p.modified_ns
                     AND p.taxonomy_revision<>?""",
                (taxonomy["revision"],),
            ).fetchone()[0])
        return {
            "total": sum(int(row["total"]) for row in rows),
            "stale_total": stale_total,
            "taxonomy_revision": taxonomy["revision"],
            "status": "local_provisional_not_ai",
            "categories": [
                {
                    "id": row["category_id"],
                    "name": names.get(row["category_id"], "旧分类，需复核"),
                    "parent": names.get(parents.get(row["category_id"]), "待复核"),
                    "count": int(row["total"]),
                }
                for row in rows
            ],
        }

    def preflight_files(
        self, category_id: str, *, offset: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        """Browse current-version local suggestions without exposing body samples."""
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("分类文件分页参数无效")
        taxonomy = load_taxonomy(self.data_root)
        by_id = {item["id"]: item for item in taxonomy["categories"]}
        selected = by_id.get(category_id)
        if selected is None:
            raise ValueError("分类不存在或已被修改")
        category_ids = (
            [category_id]
            if selected["parent_id"]
            else [item["id"] for item in taxonomy["categories"]
                  if item["parent_id"] == category_id]
        )
        if not category_ids:
            return {
                "category_id": category_id,
                "taxonomy_revision": taxonomy["revision"],
                "status": "local_provisional_not_ai",
                "total": 0,
                "offset": offset,
                "limit": limit,
                "items": [],
            }
        placeholders = ",".join("?" for _ in category_ids)
        condition = (
            "p.taxonomy_revision=? AND p.category_id IN (" + placeholders + ") "
            "AND p.outcome='sampled' AND a.state='local_inspected' "
            "AND a.byte_size=p.byte_size AND a.modified_ns=p.modified_ns"
        )
        ledger_tables = (
            "FROM local_preflight p "
            "JOIN auto_candidates a ON a.catalog_file_id=p.catalog_file_id "
        )
        params = (taxonomy["revision"], *category_ids)
        with self.connect() as db:
            self._attach_catalog(db)
            try:
                total = int(db.execute(
                    f"SELECT COUNT(*) {ledger_tables} WHERE {condition}", params
                ).fetchone()[0])
                rows = db.execute(
                    "SELECT p.catalog_file_id,f.path,p.byte_size,p.modified_ns,"
                    "p.category_id,p.coverage,"
                    "p.classification_basis,p.confidence,p.privacy_classification,"
                    "p.review_status,p.inspected_at "
                    f"{ledger_tables} JOIN catalog.files f "
                    "ON f.id=p.catalog_file_id WHERE "
                    f"{condition} AND f.state='active' "
                    "AND f.byte_size=p.byte_size AND f.modified_ns=p.modified_ns "
                    "ORDER BY p.catalog_file_id LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                ).fetchall()
            finally:
                self._detach_catalog(db)
        items = []
        for row in rows:
            item = dict(row)
            path = Path(item["path"])
            try:
                stat = path.stat()
                item["source_current"] = bool(
                    not path.is_symlink()
                    and stat.st_size == item["byte_size"]
                    and stat.st_mtime_ns == item["modified_ns"]
                )
            except (OSError, ValueError):
                item["source_current"] = False
            items.append(item)
        return {
            "category_id": category_id,
            "taxonomy_revision": taxonomy["revision"],
            "status": "local_provisional_not_ai",
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        }

    def preflight_reviews(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        """Bounded local-only diagnostic for unreadable or changed candidates."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("待复查列表参数无效")
        with self.connect() as db:
            self._attach_catalog(db)
            try:
                condition = (
                    "p.outcome='needs_review' AND a.state='needs_review' "
                    "AND a.byte_size=p.byte_size AND a.modified_ns=p.modified_ns"
                )
                reasons = db.execute(
                    "SELECT p.reason,COUNT(*) AS total FROM local_preflight p "
                    "JOIN auto_candidates a ON a.catalog_file_id=p.catalog_file_id "
                    f"WHERE {condition} GROUP BY p.reason ORDER BY total DESC"
                ).fetchall()
                rows = db.execute(
                    "SELECT f.path,p.reason,p.detected_type,p.inspected_at "
                    "FROM local_preflight p "
                    "JOIN auto_candidates a ON a.catalog_file_id=p.catalog_file_id "
                    "JOIN catalog.files f ON f.id=p.catalog_file_id "
                    f"WHERE {condition} ORDER BY p.catalog_file_id LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            finally:
                self._detach_catalog(db)
        return {
            "total": sum(int(row["total"]) for row in reasons),
            "reasons": [dict(row) for row in reasons],
            "items": [dict(row) for row in rows],
            "offset": offset,
            "limit": limit,
        }

    def preflight_batch(
        self, limit: int = 100, *, stop: threading.Event | None = None
    ) -> dict[str, Any]:
        """Light-read candidate versions without copying their body into the ledger.

        Short 25-file SQLite transactions make interruption recoverable.
        This is only a provisional local classification, never an AI decision or
        permission to import into the searchable knowledge base.
        """
        if not 1 <= limit <= 500:
            raise ValueError("本地轻读批次应在1到500个文件之间")
        self.refresh_auto_scope()
        with WindowsFileLock(self.home / "preflight.lock"):
            taxonomy = load_taxonomy(self.data_root)
            with self.connect() as db:
                self._attach_catalog(db)
                try:
                    rows = db.execute(
                        """SELECT f.id,f.path,f.scope_path,f.byte_size,f.modified_ns
                           FROM auto_candidates a
                           JOIN catalog.files f ON f.id=a.catalog_file_id
                           WHERE a.state='pending' AND f.state='active'
                             AND a.byte_size=f.byte_size
                             AND a.modified_ns=f.modified_ns
                           ORDER BY a.priority,a.catalog_file_id LIMIT ?""",
                        (limit,),
                    ).fetchall()
                    if not rows:
                        rows = db.execute(
                            """SELECT f.id,f.path,f.scope_path,f.byte_size,f.modified_ns
                               FROM auto_candidates a
                               JOIN local_preflight p
                                 ON p.catalog_file_id=a.catalog_file_id
                               JOIN catalog.files f ON f.id=a.catalog_file_id
                               WHERE a.state='local_inspected'
                                 AND p.outcome='sampled'
                                 AND p.taxonomy_revision<>?
                                 AND f.state='active'
                                 AND a.byte_size=f.byte_size
                                 AND a.modified_ns=f.modified_ns
                               ORDER BY a.priority,a.catalog_file_id LIMIT ?""",
                            (taxonomy["revision"], limit),
                        ).fetchall()
                finally:
                    self._detach_catalog(db)
            counts = {"processed": 0, "sampled": 0, "local_l0": 0, "needs_review": 0}
            pending_results: list[tuple[Any, ...]] = []

            def flush() -> None:
                if not pending_results:
                    return
                committed: list[str] = []
                # One small atomic transaction bounds crash replay to 25 files,
                # without holding a write lock while source files are read.
                with self.connect() as db:
                    for result in pending_results:
                        file_id, byte_size, modified_ns = result[:3]
                        outcome = str(result[9])
                        cursor = db.execute(
                            """UPDATE auto_candidates SET state=?
                               WHERE catalog_file_id=? AND byte_size=? AND modified_ns=?
                                 AND state IN ('pending','local_inspected')""",
                            (
                                outcome if outcome != "sampled" else "local_inspected",
                                file_id, byte_size, modified_ns,
                            ),
                        )
                        if not cursor.rowcount:
                            continue  # A concurrent job reserved this version.
                        db.execute(
                            """INSERT INTO local_preflight(
                               catalog_file_id,byte_size,modified_ns,prefix_sha256,
                               detected_type,coverage,category_id,classification_basis,
                               confidence,outcome,reason,privacy_classification,
                               review_status,inspected_at,taxonomy_revision)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(catalog_file_id) DO UPDATE SET
                               byte_size=excluded.byte_size,modified_ns=excluded.modified_ns,
                               prefix_sha256=excluded.prefix_sha256,
                               detected_type=excluded.detected_type,
                               coverage=excluded.coverage,
                               category_id=excluded.category_id,
                               classification_basis=excluded.classification_basis,
                               confidence=excluded.confidence,outcome=excluded.outcome,
                               reason=excluded.reason,
                               privacy_classification=excluded.privacy_classification,
                               review_status=excluded.review_status,
                               inspected_at=excluded.inspected_at,
                               taxonomy_revision=excluded.taxonomy_revision""",
                            result[:15],
                        )
                        committed.append(outcome)
                for outcome in committed:
                    counts["processed"] += 1
                    counts[outcome] += 1
                pending_results.clear()

            for row in rows:
                if stop is not None and stop.is_set():
                    break
                if shutil.disk_usage(self.home).free < PREFLIGHT_MIN_FREE_BYTES:
                    flush()
                    return {**counts, "paused_reason": "low_disk"}
                path = Path(row["path"])
                outcome, reason = "needs_review", "no_text_sample"
                category_id, basis, confidence = "unresolved_other", "no_body_evidence", 0.0
                privacy = "restricted" if path.suffix.casefold() in {".msg", ".eml"} else "private"
                prefix_hash, detected_type, coverage = None, None, "failed"
                try:
                    if not self._safe_preflight_path(path, Path(row["scope_path"])):
                        reason = "boundary_or_sensitive_path"
                    else:
                        record = inspect_file(path)
                        prefix_hash = record["prefix_sha256"]
                        detected_type = record["detected_type"]
                        coverage = record["coverage"]
                        if (
                            record["changed_during_read"]
                            or record["bytes"] != row["byte_size"]
                            or record["mtime_ns"] != row["modified_ns"]
                        ):
                            reason = "source_changed_since_catalog"
                        elif path.suffix.casefold() in IMAGE_SUFFIXES:
                            outcome, reason = "local_l0", "media_signature_only"
                        elif record.get("text_preview"):
                            classification = classify(record, taxonomy)
                            category_id = classification["category_id"]
                            basis = classification["basis"]
                            confidence = classification["confidence"]
                            if classification["parent_id"] in {
                                "communication", "personal", "finance", "unresolved"
                            }:
                                privacy = "restricted"
                            outcome = "sampled"
                            reason = (
                                "type_mismatch" if record["type_mismatch"]
                                else "bounded_content_sample"
                            )
                        elif "结构解析失败，待人工核查或深读" in record["notes"]:
                            reason = "structure_parse_failed"
                except OSError as error:
                    reason = (
                        "missing" if error.errno in {errno.ENOENT, errno.ENOTDIR}
                        else "permission_denied"
                        if error.errno in {errno.EACCES, errno.EPERM}
                        else "io_error"
                    )
                pending_results.append(
                    (row["id"], row["byte_size"], row["modified_ns"], prefix_hash,
                     detected_type, coverage, category_id, basis, confidence,
                     outcome, reason, privacy,
                     "needs_review" if outcome == "needs_review" else "unreviewed",
                     _now(), taxonomy["revision"])
                )
                if len(pending_results) >= 25:
                    flush()
            flush()
            return {**counts, "paused_reason": None}

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
        remote_processing: bool = False,
        local_category_id: str = "",
    ) -> list[dict[str, Any]]:
        """Reserve unseen/changed files, optionally from exact A-catalog directories.

        Automatic batches use the current processing profile. Routine code,
        media and generated files keep their L0 directory record; candidates
        still receive a bounded file read before any model recommendation.
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
        if local_category_id:
            if not automatic_only or pairs:
                raise ValueError("本地暂定分类仅支持A库普通分类批次")
            taxonomy = load_taxonomy(self.data_root)
            if not any(
                item["id"] == local_category_id and item["parent_id"]
                for item in taxonomy["categories"]
            ):
                raise ValueError("请选择当前有效的二级分类")
        if automatic_only:
            self.refresh_auto_scope()
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
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._attach_catalog(db)
            try:
                if automatic_only and not pairs:
                    if local_category_id:
                        rows = db.execute(
                            """SELECT f.id,f.path,f.scope_path,f.top_group,f.category,
                                      f.byte_size,f.modified_ns
                               FROM auto_candidates a
                               JOIN local_preflight p
                                 ON p.catalog_file_id=a.catalog_file_id
                               JOIN catalog.files f ON f.id=a.catalog_file_id
                               WHERE a.state='local_inspected' AND f.state='active'
                                 AND p.outcome='sampled' AND p.category_id=?
                                 AND p.taxonomy_revision=?
                                 AND p.byte_size=a.byte_size
                                 AND p.modified_ns=a.modified_ns
                                 AND a.byte_size=f.byte_size
                                 AND a.modified_ns=f.modified_ns
                               ORDER BY a.priority,a.catalog_file_id LIMIT ?""",
                            (local_category_id, taxonomy["revision"], limit),
                        ).fetchall()
                    else:
                        rows = db.execute(
                            """SELECT f.id,f.path,f.scope_path,f.top_group,f.category,
                                      f.byte_size,f.modified_ns
                               FROM auto_candidates a
                               JOIN catalog.files f ON f.id=a.catalog_file_id
                               WHERE a.state IN (?, 'pending') AND f.state='active'
                                 AND a.byte_size=f.byte_size
                                 AND a.modified_ns=f.modified_ns
                               ORDER BY a.priority,a.catalog_file_id LIMIT ?""",
                            ("local_inspected" if remote_processing else "pending", limit),
                        ).fetchall()
                elif pairs:
                    # idx_catalog_group(scope_path, top_group, state) keeps this
                    # bounded even when the selected plan spans hundreds of folders.
                    per_directory = min(50, max(4, ceil(candidate_window / len(pairs))))
                    rows = []
                    for scope_path, top_group in pairs:
                        if automatic_only:
                            rows.extend(
                                db.execute(
                                    """SELECT f.id,f.path,f.scope_path,f.top_group,
                                              f.category,f.byte_size,f.modified_ns,a.priority,
                                              CASE
                                                WHEN lower(f.name) LIKE 'readme%' THEN -3
                                                WHEN lower(f.name) IN (
                                                  'project.md','project_profile.md',
                                                  'architecture.md','requirements.md'
                                                ) THEN -2
                                                ELSE a.priority
                                              END AS document_priority
                                       FROM catalog.files AS f
                                       CROSS JOIN auto_candidates a
                                       WHERE f.scope_path=? AND f.top_group=?
                                         AND f.state='active'
                                         AND a.catalog_file_id=f.id
                                         AND a.state IN (?, 'pending')
                                         AND a.byte_size=f.byte_size
                                         AND a.modified_ns=f.modified_ns
                                       ORDER BY document_priority,a.priority,f.id LIMIT ?""",
                                    (
                                        scope_path, top_group,
                                        "local_inspected" if remote_processing else "pending",
                                        per_directory,
                                    ),
                                ).fetchall()
                            )
                        else:
                            rows.extend(
                                db.execute(
                                    base_sql
                                    + " AND f.scope_path=? AND f.top_group=? ORDER BY f.id LIMIT ?",
                                    (scope_path, top_group, per_directory),
                                ).fetchall()
                            )
                else:
                    rows = db.execute(
                        base_sql + " ORDER BY f.id LIMIT ?", (candidate_window,)
                    ).fetchall()
                if automatic_only and pairs:
                    # Inspect one representative per directory before taking
                    # a second from the same directory. A global id sort used
                    # to spend small AI batches on one large project clone.
                    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {
                        pair: [] for pair in pairs
                    }
                    for row in rows:
                        grouped[(row["scope_path"], row["top_group"])].append(row)
                    for bucket in grouped.values():
                        bucket.sort(
                            key=lambda row: (
                                row["document_priority"], row["priority"], row["id"]
                            )
                        )
                    rows = [
                        grouped[pair][rank]
                        for rank in range(per_directory)
                        for pair in pairs
                        if rank < len(grouped[pair])
                    ][:limit]
                elif not automatic_only:
                    rows = sorted(rows, key=self._reservation_priority)[:limit]
                timestamp = _now()
                result = []
                for row in rows:
                    if automatic_only:
                        db.execute(
                            "UPDATE auto_candidates SET state='reserved' "
                            "WHERE catalog_file_id=? AND state IN ('pending','local_inspected')",
                            (row["id"],),
                        )
                    previous = db.execute(
                        "SELECT * FROM entries WHERE catalog_file_id=?", (row["id"],)
                    ).fetchone()
                    if previous and (
                        previous["byte_size"] != row["byte_size"]
                        or previous["modified_ns"] != row["modified_ns"]
                    ):
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
                if row["ai_state"] == "unavailable":
                    ai_state = "needs_review"
                if row["ai_state"] == "local_l0":
                    ai_state = "local_l0"
                if row["ai_state"] == "rejected":
                    ai_state = "rejected"
                if row["ai_state"] == "restricted_local_only":
                    ai_state = "pending"
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
                if cursor.rowcount:
                    candidate_state = {
                        "done": "done",
                        "local_l0": "local_l0",
                        "pending": "local_inspected",
                        "needs_review": "needs_review",
                    }.get(ai_state, "failed")
                    db.execute(
                        "UPDATE auto_candidates SET state=? WHERE catalog_file_id=?",
                        (candidate_state, row["catalog_file_id"]),
                    )
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
