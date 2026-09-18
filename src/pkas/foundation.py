"""Read-only intake diagnostics. Never starts scanning, embedding or migrations."""

import ctypes
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

from pkas.ingest import SKIP_DIRECTORIES


def suggested_scopes() -> dict:
    """Enumerate folder locations/drive types only, never enumerate their contents."""
    folders = {name: str(Path.home() / name) for name in ("Desktop", "Documents", "Downloads")}
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                for name, value in {
                    "Desktop": "Desktop",
                    "Documents": "Personal",
                    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
                }.items():
                    with suppress(OSError):
                        folders[name] = os.path.expandvars(winreg.QueryValueEx(key, value)[0])
        except OSError:
            pass
    candidates = [
        {"path": path, "kind": "user_folder", "selected_by_default": True}
        for path in dict.fromkeys(folders.values())
    ]
    if os.name == "nt":
        for letter in "ABDEFGHIJKLMNOPQRSTUVWXYZ":
            path = f"{letter}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(path)) == 3:
                candidates.append(
                    {"path": path, "kind": "fixed_drive", "selected_by_default": True}
                )
    return {
        "candidates": candidates,
        "authorized": False,
        "scan_started": False,
        "note": "建议范围，不是已授权范围；当前整盘接入仍待边界验收。未读取目录内容。",
        "proposed_exclusions": sorted(
            SKIP_DIRECTORIES
            | {
                "$RECYCLE.BIN",
                "System Volume Information",
                "Windows",
                "Program Files",
                "Program Files (x86)",
                "ProgramData",
                "AppData",
            }
        ),
        "external_sources": "微信、QQ导出与移动盘需单独确认；云端处理独立授权。",
    }


def processing_state(state: str, reason: str | None) -> tuple[str, str]:
    if state == "cataloged":
        return "catalog_only", "只有目录记录，尚未建立内容索引"
    if state == "skipped" and reason == "no_indexable_text":
        return "parse_attention", "未提取到可索引文字，可能需要识别或解析修复"
    if state == "skipped":
        return "excluded", "按现有规则跳过；不代表处理成功"
    if state == "error":
        return "failed", "读取或处理失败；需按原因检查"
    if state == "missing":
        return "missing", "上次扫描未找到；本页未重新检查磁盘"
    if state == "indexed":
        return "index_recorded", "同步曾登记入库；全文与向量状态见文档对账"
    return "unknown", "状态未知"


class FoundationService:
    # The file manager is a read-only projection of the same SQLite snapshot.
    # Keep its expensive aggregate in-process only while the database/catalog
    # files are unchanged, so pagination and drive switching do not rebuild it.
    _included_snapshot_cache: ClassVar[
        dict[str, tuple[tuple[tuple[str, int, int], ...], dict]]
    ] = {}
    _included_snapshot_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, db_path: Path, machine_catalog_path: Path | None = None):
        self.db_path = db_path
        self.machine_catalog_path = machine_catalog_path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 8
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.close()

    def overview(self) -> dict:
        with self.connect() as c:
            states = {
                r[0]: r[1]
                for r in c.execute("SELECT state,count(*) FROM sync_items GROUP BY state")
            }
            roots = [
                dict(r)
                for r in c.execute(
                    "SELECT id,name,root_uri,connector_type,sync_mode,enabled,last_scan_at "
                    "FROM sync_roots"
                )
            ]
            counts = dict(
                c.execute(
                    "SELECT source_type,count(*) FROM sources "
                    "WHERE status='indexed' GROUP BY source_type"
                ).fetchall()
            )
            outbox = dict(
                c.execute("SELECT status,count(*) FROM index_outbox GROUP BY status").fetchall()
            )
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "catalog_states": states,
            "roots": roots,
            "active_source_types": counts,
            "vector_jobs": outbox,
            "notes": [
                "目录记录、文件版本、聊天消息和切片是不同计数，不能相加当覆盖率。",
                "目录状态是上次扫描结果；打开页面不会扫描、导入或调用模型。",
                "无向量登记不等于正在排队；索引台账不等于向量服务在线。",
            ],
        }

    def analytics(self, days: int = 30) -> dict:
        """Return small, read-only aggregates for the visual overview."""
        days = max(7, min(int(days), 90))
        today = datetime.now().astimezone().date()
        first_day = today - timedelta(days=days - 1)
        with self.connect() as c:
            source_types = [
                {"type": row[0], "count": int(row[1])}
                for row in c.execute(
                    "SELECT source_type,count(*) FROM sources "
                    "WHERE status='indexed' "
                    "AND source_type NOT IN ('codex-turn','thread-summary','thread-journal') "
                    "GROUP BY source_type ORDER BY count(*) DESC"
                )
            ]
            trend_rows = {
                row[0]: int(row[1])
                for row in c.execute(
                    "SELECT date(ingested_at,'localtime'),count(*) FROM sources "
                    "WHERE status='indexed' "
                    "AND source_type NOT IN ('codex-turn','thread-summary','thread-journal') "
                    "AND date(ingested_at,'localtime')>=? GROUP BY 1",
                    (first_day.isoformat(),),
                )
            }
            codex_records = int(
                c.execute(
                    "SELECT count(*) FROM sources "
                    "WHERE status='indexed' AND source_type='codex-turn'"
                ).fetchone()[0]
            )
            customer_messages = int(
                c.execute("SELECT count(*) FROM customer_messages").fetchone()[0]
            )
            depth = c.execute(
                """SELECT count(*) AS documents,
                coalesce(sum(CASE WHEN chunks>0 AND vectors=chunks THEN 1 ELSE 0 END),0),
                coalesce(sum(chunks),0),coalesce(sum(vectors),0)
                FROM (
                    SELECT s.id,count(c.id) AS chunks,count(v.chunk_id) AS vectors
                    FROM sources s
                    LEFT JOIN chunks c ON c.source_id=s.id
                    LEFT JOIN vector_index_state v ON v.chunk_id=c.id
                    WHERE s.status='indexed'
                      AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                    GROUP BY s.id
                )"""
            ).fetchone()
            original_documents = int(
                c.execute(
                    "SELECT count(*) FROM sources WHERE status='indexed' "
                    "AND source_type NOT IN ('codex-turn','thread-summary','thread-journal')"
                ).fetchone()[0]
            )
            source_aliases = int(
                c.execute(
                    """
                    SELECT count(*) FROM source_aliases a
                    JOIN sources s ON s.id=a.source_id
                    WHERE s.status='indexed'
                      AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                    """
                ).fetchone()[0]
            )
            derived_summaries = int(
                c.execute(
                    "SELECT count(*) FROM sources WHERE status='indexed' "
                    "AND source_type IN ('thread-summary','thread-journal')"
                ).fetchone()[0]
            )
        trend = []
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            trend.append({"date": day.isoformat(), "count": trend_rows.get(day.isoformat(), 0)})
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "period_days": days,
            "source_types": source_types,
            "ingest_trend": trend,
            "searchable_documents": int(depth[0]),
            "vectorized_documents": int(depth[1]),
            "original_documents": original_documents,
            "source_aliases": source_aliases,
            "represented_original_paths": original_documents + source_aliases,
            "derived_summaries": derived_summaries,
            "searchable_chunks": int(depth[2]),
            "vector_chunks": int(depth[3]),
            "vector_coverage_percent": (
                int(depth[3]) / int(depth[2]) * 100 if int(depth[2]) else 0.0
            ),
            "database_bytes": self.db_path.stat().st_size if self.db_path.is_file() else 0,
            "codex_records": codex_records,
            "customer_messages": customer_messages,
            "note": "趋势与文件类型不含 Codex 任务记录；打开页面不会扫描或调用模型。",
        }

    @staticmethod
    def _drive_key(path: str) -> str:
        match = re.match(r"^([A-Za-z]):[\\/]", path.strip())
        if not match:
            match = re.match(r"^file:/+([A-Za-z]):/", path.strip(), re.IGNORECASE)
        return match.group(1).upper() if match else "DERIVED"

    def _catalog_counts(self) -> dict[str, int]:
        path = self.machine_catalog_path
        if not path or not path.is_file():
            return {}
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            latest = connection.execute(
                "SELECT id FROM jobs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if not latest:
                return {}
            counts: dict[str, int] = {}
            for root_path, files_seen in connection.execute(
                "SELECT root_path,files_seen FROM scopes WHERE job_id=?",
                (latest[0],),
            ):
                drive = self._drive_key(str(root_path))
                counts[drive] = counts.get(drive, 0) + int(files_seen or 0)
            return counts
        finally:
            connection.close()

    def _included_snapshot_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        paths = [self.db_path, self.db_path.with_name(f"{self.db_path.name}-wal")]
        if self.machine_catalog_path:
            paths.extend(
                [
                    self.machine_catalog_path,
                    self.machine_catalog_path.with_name(
                        f"{self.machine_catalog_path.name}-wal"
                    ),
                ]
            )
        fingerprint: list[tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
                fingerprint.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
            except OSError:
                fingerprint.append((str(path), 0, 0))
        return tuple(fingerprint)

    def _included_snapshot(self) -> dict:
        key = str(self.db_path.resolve())
        fingerprint = self._included_snapshot_fingerprint()
        with self._included_snapshot_lock:
            cached = self._included_snapshot_cache.get(key)
            if cached and cached[0] == fingerprint:
                return cached[1]

        snapshot = self._included_files_uncached(
            drive="ALL",
            limit=10_000_000,
            offset=0,
        )
        # SQLite may create or advance a WAL file while opening the first
        # read transaction. Cache the post-build fingerprint, otherwise the
        # next identical page request would rebuild once for no data change.
        fingerprint = self._included_snapshot_fingerprint()
        with self._included_snapshot_lock:
            if len(self._included_snapshot_cache) >= 4:
                self._included_snapshot_cache.clear()
            self._included_snapshot_cache[key] = (fingerprint, snapshot)
        return snapshot

    def included_files(
        self,
        *,
        drive: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Show exactly which sources are represented by the searchable knowledge base."""
        snapshot = self._included_snapshot()
        selected_drive = (drive or "ALL").upper()
        all_items = cast(list[dict], snapshot["items"])
        filtered = (
            all_items
            if selected_drive == "ALL"
            else [item for item in all_items if item["drive"] == selected_drive]
        )
        level_counts = Counter(str(item["level"]) for item in filtered)
        canonical_level_counts = Counter(
            str(item["level"]) for item in filtered if not item["is_alias"]
        )
        return {
            **snapshot,
            "checked_at": datetime.now(UTC).isoformat(),
            "selected_drive": selected_drive,
            "items": filtered[offset : offset + limit],
            "total": len(filtered),
            "level_counts": dict(level_counts),
            "canonical_level_counts": dict(canonical_level_counts),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(filtered),
        }

    def _included_files_uncached(
        self,
        *,
        drive: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Build one complete file-manager snapshot from the current SQLite state."""
        with self.connect() as c:
            rows = c.execute(
                """
                SELECT s.id,s.original_name,s.original_uri,s.vault_path,s.source_type,
                       s.byte_size,s.ingested_at,
                       COUNT(DISTINCT c.id) AS chunks,
                       COUNT(DISTINCT v.chunk_id) AS vectors
                FROM sources s
                LEFT JOIN chunks c ON c.source_id=s.id
                LEFT JOIN vector_index_state v ON v.chunk_id=c.id
                WHERE s.status='indexed' AND s.source_type<>'codex-turn'
                GROUP BY s.id
                ORDER BY s.original_uri COLLATE NOCASE
                """
            ).fetchall()
            aliases = c.execute(
                """
                SELECT a.original_uri,a.original_name,a.vault_path,a.source_type,a.byte_size,
                       a.last_seen_at,a.source_id
                FROM source_aliases a
                JOIN sources s ON s.id=a.source_id
                WHERE s.status='indexed' AND s.source_type<>'codex-turn'
                ORDER BY a.original_uri COLLATE NOCASE
                """
            ).fetchall()
            customer_messages = int(
                c.execute("SELECT count(*) FROM customer_messages").fetchone()[0]
            )

        catalog_counts = self._catalog_counts()
        items = []
        disk_totals: dict[str, dict[str, int]] = {}
        canonical = {str(row["id"]): dict(row) for row in rows}
        representations: list[dict] = []
        for row in rows:
            item = dict(row)
            item["row_id"] = f"source:{item['id']}"
            item["is_alias"] = False
            item["canonical_source_type"] = item["source_type"]
            representations.append(item)
        for alias in aliases:
            item = dict(alias)
            source = canonical.get(str(item["source_id"]))
            if source is None:
                continue
            item.update(
                {
                    "id": source["id"],
                    "row_id": f"alias:{item['original_uri']}",
                    "ingested_at": source["ingested_at"],
                    "chunks": source["chunks"],
                    "vectors": source["vectors"],
                    "is_alias": True,
                    "canonical_source_type": source["source_type"],
                }
            )
            representations.append(item)
        representations.sort(key=lambda item: str(item["original_uri"]).casefold())
        for row in representations:
            item = dict(row)
            source_path = str(item["original_uri"] or "")
            key = self._drive_key(source_path)
            chunks = int(item["chunks"] or 0)
            vectors = int(item["vectors"] or 0)
            if item["canonical_source_type"] in {"thread-summary", "thread-journal"}:
                level = "summary"
            elif chunks and vectors == chunks:
                level = "semantic"
            elif chunks:
                level = "fulltext"
            else:
                level = "record_only"
            display_path = (
                str(item["vault_path"] or source_path) if key == "DERIVED" else source_path
            )
            normalized = {
                "id": item["row_id"],
                "source_id": item["id"],
                "name": item["original_name"],
                "path": display_path,
                "source_type": item["source_type"],
                "drive": key,
                "byte_size": int(item["byte_size"] or 0),
                "chunks": chunks,
                "vectors": vectors,
                "level": level,
                "ingested_at": item["ingested_at"],
                "is_alias": bool(item["is_alias"]),
            }
            items.append(normalized)
            totals = disk_totals.setdefault(
                key,
                {
                    "included_files": 0,
                    "canonical_files": 0,
                    "included_bytes": 0,
                    "fulltext_files": 0,
                    "semantic_files": 0,
                    "summary_files": 0,
                },
            )
            totals["included_files"] += 1
            if not normalized["is_alias"]:
                totals["canonical_files"] += 1
            totals["included_bytes"] += normalized["byte_size"]
            if level in {"fulltext", "semantic"}:
                totals["fulltext_files"] += 1
            if level == "semantic":
                totals["semantic_files"] += 1
            if level == "summary":
                totals["summary_files"] += 1

        keys = sorted(set(catalog_counts) | set(disk_totals) - {"DERIVED"})
        disks = []
        for key in keys:
            totals = disk_totals.get(key, {})
            total_bytes = used_bytes = free_bytes = 0
            try:
                usage = shutil.disk_usage(f"{key}:\\")
                total_bytes, used_bytes, free_bytes = usage.total, usage.used, usage.free
            except OSError:
                pass
            included_files = int(totals.get("included_files", 0))
            included_bytes = int(totals.get("included_bytes", 0))
            catalog_files = int(catalog_counts.get(key, 0))
            disks.append(
                {
                    "key": key,
                    "label": f"{key}盘",
                    "total_bytes": total_bytes,
                    "used_bytes": used_bytes,
                    "free_bytes": free_bytes,
                    "catalog_files": catalog_files,
                    "included_files": included_files,
                    "canonical_files": int(totals.get("canonical_files", 0)),
                    "included_bytes": included_bytes,
                    "fulltext_files": int(totals.get("fulltext_files", 0)),
                    "semantic_files": int(totals.get("semantic_files", 0)),
                    "summary_files": int(totals.get("summary_files", 0)),
                    "disk_share_percent": (
                        included_bytes / total_bytes * 100 if total_bytes else 0.0
                    ),
                    "file_coverage_percent": (
                        included_files / catalog_files * 100 if catalog_files else 0.0
                    ),
                }
            )
        if "DERIVED" in disk_totals:
            totals = disk_totals["DERIVED"]
            disks.append(
                {
                    "key": "DERIVED",
                    "label": "知识库派生资料",
                    "total_bytes": 0,
                    "used_bytes": 0,
                    "free_bytes": 0,
                    "catalog_files": 0,
                    **totals,
                    "disk_share_percent": 0.0,
                    "file_coverage_percent": 0.0,
                }
            )

        selected_drive = (drive or "ALL").upper()
        filtered = items if selected_drive == "ALL" else [
            item for item in items if item["drive"] == selected_drive
        ]
        level_counts = Counter(str(item["level"]) for item in filtered)
        canonical_level_counts = Counter(
            str(item["level"]) for item in filtered if not item["is_alias"]
        )
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "selected_drive": selected_drive,
            "disks": disks,
            "items": filtered[offset : offset + limit],
            "total": len(filtered),
            "canonical_total": len(rows),
            "alias_total": len(aliases),
            "level_counts": dict(level_counts),
            "canonical_level_counts": dict(canonical_level_counts),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(filtered),
            "database_bytes": self.db_path.stat().st_size if self.db_path.is_file() else 0,
            "customer_messages": customer_messages,
            "note": (
                "磁盘占比按知识库当前代表的原文件字节计算；A库只保存路径和元数据，"
                "不会复制数百万个原文件。派生摘要单独列出，避免冒充原盘资料。"
            ),
        }

    def documents(self, limit: int = 50, offset: int = 0) -> dict:
        with self.connect() as c:
            total = c.execute(
                "SELECT count(*) FROM sources WHERE status='indexed' AND source_type<>'codex-turn'"
            ).fetchone()[0]
            sources = c.execute(
                "SELECT s.id,s.original_name,s.original_uri,s.source_type,s.metadata_json,"
                "d.parser_version "
                "FROM sources s JOIN documents d ON d.source_id=s.id "
                "WHERE s.status='indexed' AND s.source_type<>'codex-turn' "
                "ORDER BY s.id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total_chunks = int(c.execute("SELECT count(*) FROM chunks").fetchone()[0])
            total_fts_rows = int(c.execute("SELECT count(*) FROM chunks_fts").fetchone()[0])
            # chunks_fts is a virtual table whose rowid is deliberately unrelated
            # to chunks.rowid.  Fetching every unindexed chunk_id for each ledger
            # page used to cost a full 190k-row materialization.  The repository
            # writes chunks and FTS entries in the same transaction, so expose the
            # fast database-wide count consistency status and keep per-file counts
            # for chunks/vector records only.
            fts_consistent = total_chunks == total_fts_rows
            source_ids = [str(source["id"]) for source in sources]
            chunks_by_source: dict[str, list[sqlite3.Row]] = {
                source_id: [] for source_id in source_ids
            }
            if source_ids:
                placeholders = ",".join("?" for _ in source_ids)
                rows = c.execute(
                    "SELECT c.id,c.source_id,c.chunker_version,v.chunk_id AS vector_id "
                    "FROM chunks c LEFT JOIN vector_index_state v ON v.chunk_id=c.id "
                    f"WHERE c.source_id IN ({placeholders})",
                    source_ids,
                ).fetchall()
                for row in rows:
                    chunks_by_source[str(row["source_id"])].append(row)
            items = []
            for source in sources:
                chunks = chunks_by_source[str(source["id"])]
                n = len(chunks)
                vectors = sum(r["vector_id"] is not None for r in chunks)
                try:
                    extraction = json.loads(source["metadata_json"]).get("extraction", {})
                    quality = extraction.get("initial_quality", {})
                    quality_reasons = [
                        str(reason)
                        for reason in quality.get("reasons", [])
                        if isinstance(reason, str)
                    ][:12]
                    visual_pages = sorted(
                        {
                            int(page)
                            for page in quality.get("visual_pages", [])
                            if str(page).isdigit() and int(page) > 0
                        }
                    )[:50]
                    attention = bool(
                        extraction.get("warnings")
                        or quality_reasons
                        or extraction.get("block_index_truncated")
                    )
                except (ValueError, TypeError, AttributeError):
                    extraction, attention, quality_reasons, visual_pages = {}, True, [], []
                items.append(
                    {
                        "id": source["id"],
                        "name": source["original_name"],
                        "path": source["original_uri"],
                        "type": source["source_type"],
                        "chunks": n,
                        "fts_chunks": n if fts_consistent else None,
                        "vector_chunks": vectors,
                        "fulltext": "indexed" if n and fts_consistent else "needs_global_check",
                        "vector": "recorded" if n and vectors == n else "not_fully_recorded",
                        "quality": "attention" if attention else "not_manually_verified",
                        "quality_reasons": quality_reasons,
                        "visual_pages": visual_pages,
                        "typed_v2": sum(r["chunker_version"] == "typed-v2" for r in chunks),
                        "parser_version": source["parser_version"],
                        "parsing": (
                            "current"
                            if source["parser_version"] == "hybrid-v2"
                            else "legacy_or_unverified"
                        ),
                    }
                )
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "note": (
                "全文索引存在不证明提取完整；当前页按全库切片/FTS 行数一致性显示，"
                "不再为每次翻页扫描全部 FTS ID。向量仅核对登记数量，不证明服务在线。"
            ),
            "fts_consistent": fts_consistent,
            "total_chunks": total_chunks,
            "total_fts_rows": total_fts_rows,
        }

    def files(self, *, root_id: str, state: str, limit: int = 50, offset: int = 0) -> dict:
        if state not in {"cataloged", "indexed", "skipped", "missing", "error"}:
            raise ValueError("无效状态")
        with self.connect() as c:
            if not c.execute("SELECT 1 FROM sync_roots WHERE id=?", (root_id,)).fetchone():
                raise ValueError("资料源不存在")
            rows = c.execute(
                "SELECT id,relative_path,state,reason,last_seen_at,source_id "
                "FROM sync_items WHERE root_id=? AND state=? ORDER BY rowid "
                "LIMIT ? OFFSET ?",
                (root_id, state, limit + 1, offset),
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["stage"], item["explanation"] = processing_state(item["state"], item["reason"])
            # Do not echo arbitrary exception bodies; only stored safe codes.
            reason = item.get("reason") or ""
            item["reason"] = (
                reason if reason.replace("_", "").isalnum() and len(reason) < 64 else "需本机核查"
            )
            items.append(item)
        return {"items": items, "has_more": len(rows) > limit, "offset": offset}
