"""Durable, bounded-memory file inspection and agent summary jobs; originals stay untouched."""

import errno
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from ctypes import windll
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from pkas.auto_promotion import AutoPromotionService
from pkas.catalog_classification import (
    AI_PROTOCOL,
    ALL_INSPECTED_SELECTION_ID,
    CatalogClassificationLedger,
    trusted_recommended_mode,
)
from pkas.catalog_scope import (
    DOCUMENT_SUFFIXES,
    IMAGE_SUFFIXES,
    MARKDOWN_SUFFIXES,
    load_auto_policy,
)
from pkas.codex_capture import redact_secrets
from pkas.content_taxonomy import atomic_write, classify, load_taxonomy
from pkas.db import Database
from pkas.directory_summary import authorize_root
from pkas.file_inspector import inspect_file
from pkas.ingest import is_sensitive_path
from pkas.intake import EXCLUDED, linked
from pkas.llm import DeepSeekGateway, LLMError
from pkas.local_lock import WindowsFileLock
from pkas.repository import Repository
from pkas.summary_agent import INSTRUCTIONS, AgentError, CodexAgent


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def markdown_literal(value):
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", text)


def inspection_failure_kind(error: OSError) -> str:
    """Keep retry diagnostics useful without storing paths or OS error text."""
    message = str(error).casefold()
    if "boundary changed" in message:
        return "boundary_changed"
    if "changed during read" in message:
        return "changed_during_read"
    if error.errno in {errno.ENOENT, errno.ENOTDIR}:
        return "missing"
    if error.errno in {errno.EACCES, errno.EPERM}:
        return "permission_denied"
    if error.errno in {errno.EBUSY, errno.ETXTBSY}:
        return "temporarily_locked"
    return "io_error"


AGENT_RETRY_VALIDATION_FEEDBACK = (
    "上次JSON未通过本地校验。必须为每个输入id返回且只返回一项；"
    "category_id必须来自给定分类；summary、purpose、recommendation_reason、"
    "evidence、uncertainty均为字符串；secondary_category_ids必须是最多3项、"
    "不含主分类的给定分类ID列表；importance只能是high/normal/low；"
    "recommended_mode只能是catalog/extract/full/semantic。"
    "evidence必须逐字、连续存在于对应sample中且不超过40字符；"
    "否则选择unresolved_other并返回空evidence；没有正文样本的文件只能选择"
    "unresolved_other、recommended_mode=catalog和空evidence。"
    "这是唯一一次纠错重试。"
)

class SummaryJobRequest(BaseModel):
    path: str = Field(default="", max_length=1000)
    scope: Literal[
        "auto", "path", "all_local_drives", "catalog_batch", "auto_promotion_batch"
    ] = "auto"
    provider: Literal["local", "codex", "deepseek", "external"] = "local"
    model: str = Field(default="gpt-5.6-luna", max_length=100)
    codex_executable: str = Field(default="", max_length=1000)
    allow_remote_processing: bool = False
    allow_restricted_remote_processing: bool = False
    batch_size: int = Field(default=5, ge=1, le=20)
    max_ai_batches: int = Field(default=20, ge=1, le=10000)
    catalog_batch_size: int = Field(default=50, ge=1, le=500)
    local_category_id: str = Field(default="", max_length=64)
    auto_promotion_id: str = Field(default="", max_length=32)


def local_fixed_roots() -> list[Path]:
    """Return ready local fixed volumes without walking or reading their contents."""
    if os.name != "nt":
        raise ValueError("整机范围目前仅支持Windows本地固定磁盘")
    mask = windll.kernel32.GetLogicalDrives()
    roots = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = Path(f"{chr(65 + index)}:\\")
        # DRIVE_FIXED=3. Network, optical and removable volumes are not implicit scope.
        if windll.kernel32.GetDriveTypeW(str(root)) == 3 and root.is_dir():
            roots.append(root)
    if not roots:
        raise ValueError("没有发现可用的本地固定磁盘")
    return roots


class SummaryEdit(BaseModel):
    category_id: str
    summary: str = Field(max_length=2000)
    revision: int


class SummaryExport(BaseModel):
    destination: str = Field(min_length=1, max_length=1000)


class SummaryPromotion(BaseModel):
    category_ids: list[str] = Field(default_factory=list, max_length=50)
    include_all_inspected: bool = False
    # Keep the same four meanings in the classified-assembly API and the
    # ordinary intake page.  ``extract`` is L1: a local, partial excerpt with
    # source/hash provenance, not an AI summary or full-text import.
    mode: Literal["catalog", "extract", "full", "semantic", "recommended"] = "full"

    @model_validator(mode="after")
    def validate_selection(self):
        if ALL_INSPECTED_SELECTION_ID in self.category_ids:
            self.include_all_inspected = True
            self.category_ids = [
                category_id
                for category_id in self.category_ids
                if category_id != ALL_INSPECTED_SELECTION_ID
            ]
        if not self.category_ids and not self.include_all_inspected:
            raise ValueError("请选择至少一个分类，或选择全部已检查资料")
        if len(self.category_ids) != len(set(self.category_ids)):
            raise ValueError("分类不能重复选择")
        return self


class SummaryJobs:
    def __init__(self, settings):
        self.settings = settings
        self.home = settings.data_root / "summary-jobs"
        self.active: dict[
            str,
            tuple[
                threading.Thread,
                threading.Event,
                threading.Event,
                threading.Event,
            ],
        ] = {}
        self.guard = threading.RLock()
        self.catalog_ledger = CatalogClassificationLedger(settings.data_root)
        self.auto_promotion = AutoPromotionService(settings)
        self._preflight_thread: threading.Thread | None = None
        self._preflight_stop = threading.Event()
        self._preflight_state = "idle"
        self._preflight_processed = 0
        self._preflight_error: str | None = None

    def directory(self, job):
        if len(job) != 32 or any(c not in "0123456789abcdef" for c in job):
            raise ValueError("无效整理任务编号")
        return self.home / job

    @contextmanager
    def db(self, job):
        path = self.directory(job) / "queue.sqlite"
        if not path.is_file():
            raise ValueError("整理任务不存在")
        db = sqlite3.connect(path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def meta(self, db):
        return json.loads(db.execute("SELECT value FROM meta WHERE id=1").fetchone()[0])

    def put(self, db, meta):
        meta["updated_at"] = datetime.now(UTC).isoformat()
        db.execute("UPDATE meta SET value=? WHERE id=1", (dumps(meta),))

    @staticmethod
    def roots(meta) -> list[Path]:
        values = meta.get("roots") or [meta["root"]]
        return [Path(value).resolve(strict=False) for value in values]

    @staticmethod
    def containing_root(path: Path, roots: list[Path]) -> Path | None:
        resolved = path.resolve(strict=False)
        matches = [root for root in roots if resolved == root or root in resolved.parents]
        return max(matches, key=lambda root: len(root.parts), default=None)

    def request_roots(self, request: SummaryJobRequest) -> tuple[str, list[Path]]:
        scope = request.scope
        if scope == "catalog_batch":
            return "A库下一批分类文件", []
        if scope == "auto_promotion_batch":
            return "Luna自动选择的待检查文件", []
        if scope == "auto":
            scope = "path" if request.path.strip() else "all_local_drives"
        if scope == "all_local_drives":
            roots = local_fixed_roots()
            return "所有本地固定磁盘", roots
        if not request.path.strip():
            raise ValueError("请选择资料目录，或使用所有本地固定磁盘")
        root = authorize_root(Path(request.path).expanduser())
        return str(root), [root]

    def create(self, request: SummaryJobRequest, start=True):
        if self._preflight_thread and self._preflight_thread.is_alive():
            raise ValueError("本地逐文件轻读正在运行，请先暂停后再创建AI批次")
        root_label, roots = self.request_roots(request)
        auto_scope = None
        if request.scope == "auto_promotion_batch":
            if not request.auto_promotion_id:
                raise ValueError("请选择已完成的自动选择计划")
            if request.provider != "codex" or not request.allow_remote_processing:
                raise ValueError("自动选择后的文件复核必须明确使用Luna并开启云端处理")
            auto_scope = self.auto_promotion.inspection_scope(request.auto_promotion_id)
        if request.local_category_id and request.scope != "catalog_batch":
            raise ValueError("按暂定分类筛选仅适用于A库分类批次")
        data = self.settings.data_root.resolve()
        if any(root == data or data in root.parents for root in roots):
            raise ValueError("不能整理知识库自身派生数据")
        if request.provider != "local" and not request.allow_remote_processing:
            raise ValueError("AI处理会发送内容样本，请明确勾选后开始")
        if request.provider == "codex":
            from pkas.summary_agent import codex_binary

            codex_binary(request.codex_executable)
        if request.provider == "deepseek" and not self.settings.deepseek_enabled:
            raise ValueError("DeepSeek API 尚未在本机安全配置中启用")
        if request.provider == "deepseek" and request.batch_size > 1:
            # The fallback has a stricter evidence contract than a free-form chat.
            # One document per request makes its output auditable and prevents a
            # malformed multi-item reply from discarding otherwise good local work.
            request = request.model_copy(update={"batch_size": 1})
        if request.scope == "auto_promotion_batch" and request.provider == "codex":
            # These are the high-value files selected from the whole-machine
            # directory plan.  One malformed multi-file reply must not reject
            # otherwise useful documents in the same packet.
            request = request.model_copy(update={"batch_size": 1})
        if request.scope == "catalog_batch":
            # Resolve the current profile and catalog generation before creating
            # a task directory. Building the candidate table never reads bodies.
            self.catalog_ledger.refresh_auto_scope()
        job = uuid.uuid4().hex
        folder = self.directory(job)
        folder.mkdir(parents=True)
        # Independent DB: no migrations, triggers, cascades or main DB writes.
        with closing(sqlite3.connect(folder / "queue.sqlite")) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE meta(id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE dirs(path TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending');
                CREATE INDEX dirs_state ON dirs(state,path);
                CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', record TEXT, summary TEXT,
                    ai_state TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX files_state ON files(state,id);
                CREATE INDEX files_ai ON files(ai_state,id);
                CREATE TABLE packets(id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT, thread_id TEXT);
            """)
            meta = {
                "id": job,
                "root": root_label,
                "roots": [str(root) for root in roots],
                "scope": (
                    "catalog_batch"
                    if request.scope in {"catalog_batch", "auto_promotion_batch"}
                    else "all_local_drives"
                    if len(roots) > 1 or request.scope == "all_local_drives"
                    else "path"
                ),
                "request": request.model_dump(),
                "taxonomy": load_taxonomy(self.settings.data_root),
                "stage": (
                    "inspect"
                    if request.scope in {"catalog_batch", "auto_promotion_batch"}
                    else "discover"
                ),
                "state": "paused",
                "created_at": datetime.now(UTC).isoformat(),
                "excluded": 0,
                "directory_errors": 0,
                "ai_batches": 0,
                "message": "已创建本地持久任务；原件只读",
                "exports": [],
                "scanner": "machine_catalog_batch"
                if request.scope in {"catalog_batch", "auto_promotion_batch"}
                else "native_resumable",
                "md_path": None,
            }
            db.execute("INSERT INTO meta VALUES(1,?)", (dumps(meta),))
            if request.scope not in {"catalog_batch", "auto_promotion_batch"}:
                db.executemany("INSERT INTO dirs(path) VALUES(?)", [(str(root),) for root in roots])
        if request.scope in {"catalog_batch", "auto_promotion_batch"}:
            try:
                rows = self.catalog_ledger.reserve(
                    job,
                    request.catalog_batch_size,
                    directory_units=auto_scope["units"] if auto_scope else None,
                    automatic_only=True,
                    remote_processing=request.provider != "local",
                    local_category_id=request.local_category_id,
                )
                if not rows:
                    raise ValueError("所选A库范围没有待分类或已变化的文件；无需新建批次")
            except Exception:
                # This exact UUID folder was created by this call and has only
                # the empty queue DB. Do not leave ghost tasks in the sidebar.
                if folder.parent.resolve() == self.home.resolve() and folder.name == job:
                    queue = folder / "queue.sqlite"
                    for suffix in ("", "-wal", "-shm"):
                        Path(str(queue) + suffix).unlink(missing_ok=True)
                    folder.rmdir()  # Refuses to remove unexpected user files.
                raise
            with self.db(job) as db:
                db.execute("ALTER TABLE files ADD COLUMN catalog_file_id INTEGER")
                db.execute("CREATE UNIQUE INDEX files_catalog_file_id ON files(catalog_file_id)")
                db.executemany(
                    "INSERT INTO files(path,catalog_file_id) VALUES(?,?)",
                    [(row["path"], row["id"]) for row in rows],
                )
                meta = self.meta(db)
                meta["roots"] = sorted({row["scope_path"] for row in rows})
                meta["root"] = f"A库分类批次（{len(rows)}个文件）"
                meta["catalog_batch"] = {
                    "requested": request.catalog_batch_size,
                    "reserved": len(rows),
                    "ledger_path": str(self.catalog_ledger.path),
                }
                if auto_scope:
                    meta["auto_promotion"] = {
                        "id": auto_scope["auto_promotion_id"],
                        "overview_id": auto_scope["overview_id"],
                        "eligible_directory_units": len(auto_scope["units"]),
                        "file_reading": "none",
                    }
                meta["message"] = "已从A库保留下一批文件；仅在点击继续后本地轻读"
                self.put(db, meta)
        if start:
            self.resume(job)
        return self.view(job)

    def recent(self):
        if not self.home.exists():
            return []
        folders = sorted(self.home.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        return [self.view(p.name, limit=0) for p in folders[:30] if (p / "queue.sqlite").is_file()]

    def catalog_progress(self):
        self.catalog_ledger.schedule_auto_scope_refresh()
        return self.catalog_ledger.progress()

    def catalog_preflight_status(self):
        with self.guard:
            return {
                "state": self._preflight_state,
                "running": bool(self._preflight_thread and self._preflight_thread.is_alive()),
                "processed_this_run": self._preflight_processed,
                "error_kind": self._preflight_error,
            }

    def catalog_preflight_categories(self):
        return self.catalog_ledger.preflight_categories()

    def catalog_preflight_files(self, category_id, *, offset=0, limit=20):
        return self.catalog_ledger.preflight_files(
            category_id, offset=offset, limit=limit
        )

    def catalog_preflight_reviews(self, *, offset=0, limit=20):
        return self.catalog_ledger.preflight_reviews(offset=offset, limit=limit)

    def start_catalog_preflight(self):
        # Explicit user action only: no file body is read on page load/startup.
        self.catalog_ledger.refresh_auto_scope()
        with self.guard:
            if self._preflight_thread and self._preflight_thread.is_alive():
                return self.catalog_preflight_status()
            if any(
                not finished.is_set() and (thread.is_alive() or not started.is_set())
                for thread, _, started, finished in self.active.values()
            ):
                raise ValueError("已有AI整理任务运行，请完成或暂停后再本地轻读")
            self._preflight_stop = threading.Event()
            self._preflight_state = "running"
            self._preflight_processed = 0
            self._preflight_error = None
            self._preflight_thread = threading.Thread(
                target=self._run_catalog_preflight, daemon=True
            )
            self._preflight_thread.start()
            return self.catalog_preflight_status()

    def pause_catalog_preflight(self):
        with self.guard:
            if self._preflight_thread and self._preflight_thread.is_alive():
                self._preflight_stop.set()
            return self.catalog_preflight_status()

    def _run_catalog_preflight(self):
        try:
            while not self._preflight_stop.is_set():
                batch = self.catalog_ledger.preflight_batch(
                    100, stop=self._preflight_stop
                )
                with self.guard:
                    self._preflight_processed += batch["processed"]
                if batch["paused_reason"]:
                    with self.guard:
                        self._preflight_state = "low_disk"
                    return
                if not batch["processed"]:
                    progress = self.catalog_ledger.progress()
                    with self.guard:
                        self._preflight_state = (
                            "completed" if progress["local_read_pending"] == 0
                            else "waiting_for_catalog_or_job"
                        )
                    return
        except Exception as error:
            with self.guard:
                self._preflight_state = "error"
                self._preflight_error = type(error).__name__
        finally:
            if self._preflight_stop.is_set():
                with self.guard:
                    self._preflight_state = "paused"

    def retry_catalog_progress(self):
        self.catalog_ledger.retry_auto_scope_refresh()
        return self.catalog_ledger.progress()

    def catalog_classification_counts(self):
        return self.catalog_ledger.classification_counts(load_taxonomy(self.settings.data_root))

    def catalog_promotion_selection(self, payload: SummaryPromotion) -> dict:
        taxonomy = load_taxonomy(self.settings.data_root)
        leaf_ids = {item["id"] for item in taxonomy["categories"] if item.get("parent_id")}
        unknown = set(payload.category_ids) - leaf_ids
        if unknown:
            raise ValueError("所选分类已变化，请刷新后重新选择")
        return self.catalog_ledger.selection(
            payload.category_ids,
            payload.mode,
            include_all_inspected=payload.include_all_inspected,
        )

    def view(self, job, offset=0, limit=50):
        with self.db(job) as db:
            meta = self.meta(db)
            handle = self.active.get(job)
            # The read transaction may have started just before the worker
            # committed its terminal state. Refresh before deriving counts and
            # records so the returned envelope is internally consistent.
            if handle and handle[3].is_set():
                db.rollback()
                meta = self.meta(db)
            meta["counts"] = dict(db.execute("SELECT state,COUNT(*) FROM files GROUP BY state"))
            meta["ai_counts"] = dict(
                db.execute("SELECT ai_state,COUNT(*) FROM files GROUP BY ai_state")
            )
            meta["pending_directories"] = db.execute(
                "SELECT COUNT(*) FROM dirs WHERE state='pending'"
            ).fetchone()[0]
            meta["records"] = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM files ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
                )
            ]
            for row in meta["records"]:
                row["record"] = json.loads(row["record"]) if row["record"] else None
                row["summary"] = json.loads(row["summary"]) if row["summary"] else None
            # The worker can finish between the first metadata read and the
            # end of the record query. Recheck the barrier once more; otherwise
            # this read could combine a terminal thread with a pre-terminal
            # SQLite snapshot and report a false interruption.
            latest_handle = self.active.get(job)
            if latest_handle and latest_handle[3].is_set():
                db.rollback()
                meta = self.meta(db)
                meta["counts"] = dict(
                    db.execute("SELECT state,COUNT(*) FROM files GROUP BY state")
                )
                meta["ai_counts"] = dict(
                    db.execute("SELECT ai_state,COUNT(*) FROM files GROUP BY ai_state")
                )
                meta["pending_directories"] = db.execute(
                    "SELECT COUNT(*) FROM dirs WHERE state='pending'"
                ).fetchone()[0]
                meta["records"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM files ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
                    )
                ]
                for row in meta["records"]:
                    row["record"] = json.loads(row["record"]) if row["record"] else None
                    row["summary"] = json.loads(row["summary"]) if row["summary"] else None
                handle = latest_handle
            if meta["stage"] == "done":
                meta["classification_counts"] = meta.get(
                    "classification_counts"
                ) or self._classification_counts(db, meta)
            # A just-started Windows thread can briefly report is_alive=False
            # before its target has entered Python. The start barrier prevents
            # that tiny window from being shown as an interruption.
            meta["running"] = bool(
                handle
                and not handle[3].is_set()
                and (handle[0].is_alive() or not handle[2].is_set())
            )
            if meta["state"] == "running" and not meta["running"]:
                meta["state"] = "paused"
                meta["message"] = "上次运行中断，进度已保留；点击继续，不会自动重复全部读取"
            return meta

    @staticmethod
    def _classification_counts(db, meta):
        labels = {
            item["id"]: item["name"]
            for item in meta["taxonomy"]["categories"]
            if item.get("parent_id")
        }
        totals = {}
        for row in db.execute("SELECT record FROM files WHERE state='inspected'"):
            record = json.loads(row["record"])
            category_id = record.get("classification", {}).get(
                "category_id", "unresolved_other"
            )
            current = totals.setdefault(
                category_id,
                {"id": category_id, "name": labels.get(category_id, category_id), "count": 0,
                 "bytes": 0},
            )
            current["count"] += 1
            current["bytes"] += int(record.get("bytes", 0))
        return sorted(totals.values(), key=lambda item: (-item["count"], item["name"]))

    def promotion_selection(self, job: str, payload: SummaryPromotion) -> dict:
        """Return classified source identities; the intake service revalidates every file."""
        with self.db(job) as db:
            meta = self.meta(db)
            if meta["stage"] != "done" or meta["state"] not in {"completed", "warning"}:
                raise ValueError("请先完成本次全盘分类，再选择后续处理方式")
            leaf_ids = {
                item["id"]
                for item in meta["taxonomy"]["categories"]
                if item.get("parent_id")
            }
            unknown = set(payload.category_ids) - leaf_ids
            if unknown:
                raise ValueError("所选分类已变化，请刷新后重新选择")
            items = []
            recommendation_unavailable = 0
            selected = set(payload.category_ids)
            for row in db.execute(
                "SELECT id,path,record,revision FROM files WHERE state='inspected' ORDER BY id"
            ):
                record = json.loads(row["record"])
                classification = record.get("classification", {})
                categories_for_file = {
                    classification.get("category_id"),
                    *classification.get("secondary_category_ids", []),
                }
                if not payload.include_all_inspected and not (categories_for_file & selected):
                    continue
                recommendation = trusted_recommended_mode(record)
                if payload.mode == "recommended" and recommendation is None:
                    recommendation_unavailable += 1
                    continue
                items.append(
                    {
                        "summary_file_id": row["id"],
                        "path": row["path"],
                        "relative": record.get("relative") or row["path"],
                        "bytes": int(record.get("bytes", 0)),
                        "mtime_ns": int(record.get("mtime_ns", 0)),
                        "content_category_id": classification.get("category_id"),
                        "content_category_label": classification.get("label"),
                        "classification_basis": classification.get("basis", "AI或规则分类"),
                        "classification_revision": row["revision"],
                        "ai_understanding": record.get("understanding"),
                        "recommended_action": recommendation,
                    }
                )
        if not items:
            if payload.mode == "recommended":
                raise ValueError("所选范围没有可核验的AI处理建议，请先完成Luna理解")
            raise ValueError("所选分类中没有可处理文件")
        return {
            "source_summary_job": job,
            "root": meta["root"],
            "roots": meta.get("roots") or [meta["root"]],
            "mode": payload.mode,
            "category_ids": payload.category_ids,
            "include_all_inspected": payload.include_all_inspected,
            "recommendation_unavailable": recommendation_unavailable,
            "items": items,
        }

    def resume(self, job):
        with self.guard:
            if self._preflight_thread and self._preflight_thread.is_alive():
                raise ValueError("本地逐文件轻读正在运行，请先暂停后再启动AI整理")
            if any(
                not finished.is_set()
                and (thread.is_alive() or not started.is_set())
                for thread, _, started, finished in self.active.values()
            ):
                raise ValueError("已有整理任务运行，请先暂停；默认串行以免拖慢电脑")
            # Cross-process lock prevents two desktop instances from paying for the same batch.
            lock = WindowsFileLock(self.directory(job) / "worker.lock")
            if not lock.acquire():
                raise ValueError("另一个进程正在处理该任务")
            with self.db(job) as db:
                meta = self.meta(db)
                if meta["stage"] == "done":
                    lock.release()
                    raise ValueError("已完成，无需重复；源文件更新后可新建整理任务")
                meta["state"] = "running"
                meta["message"] = "正在继续本地记录；中断的AI批次会重试，可能产生额外额度消耗"
                self.put(db, meta)
            stop = threading.Event()
            started = threading.Event()
            finished = threading.Event()
            thread = threading.Thread(
                target=self._run, args=(job, stop, lock, started, finished), daemon=True
            )
            self.active[job] = (thread, stop, started, finished)
            thread.start()

    def pause(self, job):
        with self.guard:
            if job in self.active:
                self.active[job][1].set()

    def retry_errors(self, job):
        with self.guard:
            if any(
                not finished.is_set()
                and (thread.is_alive() or not started.is_set())
                for thread, _, started, finished in self.active.values()
            ):
                raise ValueError("请先暂停正在运行的整理任务")
            with WindowsFileLock(self.directory(job) / "worker.lock"), self.db(job) as db:
                db.execute("BEGIN IMMEDIATE")
                meta = self.meta(db)
                db.execute("UPDATE dirs SET state='pending' WHERE state='error'")
                db.execute(
                    "UPDATE files SET state='pending',ai_state='pending' "
                    "WHERE state IN ('error','missing') OR ai_state='stale'"
                )
                db.execute("UPDATE packets SET state='invalid' WHERE state='pending'")
                meta.update(
                    stage="discover",
                    state="paused",
                    directory_errors=0,
                    message="异常项已重新排队，正常文件保留",
                )
                self.put(db, meta)
            self.resume(job)

    def close(self):
        self._preflight_stop.set()
        if self._preflight_thread is not None:
            self._preflight_thread.join(timeout=8)
        for _, stop, _, _ in self.active.values():
            stop.set()
        for thread, _, _, _ in self.active.values():
            thread.join(timeout=8)

    def _set(self, job, **values):
        with self.db(job) as db:
            meta = self.meta(db)
            meta.update(values)
            self.put(db, meta)

    def _safe(self, path, roots):
        resolved = path.resolve(strict=False)
        data = self.settings.data_root.resolve()
        return (
            self.containing_root(resolved, roots) is not None
            and not linked(path)
            and data not in (resolved, *resolved.parents)
            and not is_sensitive_path(path)
            and not any(part.casefold() in EXCLUDED for part in path.parts)
        )

    def _discover(self, job, stop):
        while not stop.is_set():
            with self.db(job) as db:
                meta = self.meta(db)
                row = db.execute("SELECT path FROM dirs WHERE state='pending' LIMIT 1").fetchone()
            if row is None:
                self._set(job, stage="inspect")
                return
            directory = Path(row[0])
            roots = self.roots(meta)
            try:
                if not self._safe(directory, roots):
                    raise OSError("unsafe directory")
                with os.scandir(directory) as entries:
                    batch = []
                    excluded = 0
                    for entry in entries:
                        if stop.is_set():
                            return  # directory remains pending; UNIQUE paths deduplicate replay
                        path = Path(entry.path)
                        if not self._safe(path, roots):
                            excluded += 1
                            continue
                        batch.append((str(path), entry.is_dir(follow_symlinks=False)))
                        if len(batch) >= 200:
                            self._discover_batch(job, batch)
                            batch = []
                    self._discover_batch(job, batch)
                with self.db(job) as db:
                    db.execute("UPDATE dirs SET state='done' WHERE path=?", (str(directory),))
                    meta = self.meta(db)
                    meta["excluded"] += excluded
                    self.put(db, meta)
            except OSError:
                with self.db(job) as db:
                    db.execute("UPDATE dirs SET state='error' WHERE path=?", (str(directory),))
                    meta = self.meta(db)
                    meta["directory_errors"] += 1
                    self.put(db, meta)

    def _discover_batch(self, job, batch):
        with self.db(job) as db:
            for path, is_dir in batch:
                table = "dirs" if is_dir else "files"
                db.execute(f"INSERT OR IGNORE INTO {table}(path) VALUES(?)", (path,))

    def _refresh_changed(self, job, stop):
        """Resume verifies cheap version metadata, not every file's body again."""
        last = 0
        changed = False
        while not stop.is_set():
            with self.db(job) as db:
                rows = db.execute(
                    "SELECT id,path,record FROM files WHERE id>? AND state='inspected' "
                    "ORDER BY id LIMIT 100",
                    (last,),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                last = row["id"]
                record = json.loads(row["record"])
                try:
                    stat = Path(row["path"]).stat()
                    same = stat.st_size == record.get("bytes") and stat.st_mtime_ns == record.get(
                        "mtime_ns"
                    )
                except OSError:
                    same = False
                if not same:
                    changed = True
                    with self.db(job) as db:
                        db.execute(
                            "UPDATE files SET state='pending',ai_state='pending' WHERE id=?",
                            (last,),
                        )
            if stop.is_set():
                break
        if changed:
            with self.db(job) as db:
                db.execute("UPDATE packets SET state='invalid' WHERE state='pending'")
                meta = self.meta(db)
                meta["stage"] = "inspect"
                meta["message"] = "检测到源文件变化，只重新读取变化的文件"
                self.put(db, meta)

    def _inspect(self, job, stop):
        with self.db(job) as db:
            meta = self.meta(db)
        roots = self.roots(meta)
        while not stop.is_set():
            with self.db(job) as db:
                rows = db.execute(
                    "SELECT id,path,record,summary FROM files WHERE state='pending' LIMIT 50"
                ).fetchall()
            if not rows:
                self._set(
                    job, stage="agent" if meta["request"]["provider"] != "local" else "markdown"
                )
                return
            for row in rows:
                if stop.is_set():
                    return
                path = Path(row["path"])
                try:
                    if not self._safe(path, roots):
                        raise OSError("boundary changed")
                    record = inspect_file(path)
                    if record.get("changed_during_read"):
                        raise OSError("changed during read")
                    record["classification"] = classify(record, meta["taxonomy"])
                    state = "inspected"
                except OSError as error:
                    failure_kind = inspection_failure_kind(error)
                    record = {
                        "coverage": "failed",
                        "notes": ["无法读取或读取期间变化"],
                        "failure_kind": failure_kind,
                        "classification": classify({}, meta["taxonomy"]),
                    }
                    state = "missing" if failure_kind == "missing" else "error"
                root = self.containing_root(path, roots)
                record["relative"] = (
                    str(path.relative_to(root))
                    if len(roots) == 1 and root is not None
                    else str(path)
                )
                record["inspected_at"] = datetime.now(UTC).isoformat()
                classification = record["classification"]
                restricted = (
                    path.suffix.casefold() in {".msg", ".eml"}
                    or classification.get("parent_id") in {
                        "communication", "personal", "finance"
                    }
                    or (
                        classification.get("parent_id") == "unresolved"
                        and bool(record.get("text_preview"))
                    )
                )
                record["privacy_classification"] = (
                    "restricted" if restricted else "private"
                )
                summary = {
                    "text": "本地内容抽样已完成，等待AI理解用途与建议处理深度。",
                    "origin": "local",
                    "review_status": "unreviewed",
                }
                if row["record"]:
                    record["previous_version"] = {
                        "record": json.loads(row["record"]),
                        "summary": json.loads(row["summary"]) if row["summary"] else None,
                    }
                    # Keep only the immediate predecessor rather than nesting indefinitely.
                    record["previous_version"]["record"].pop("previous_version", None)
                automatic_batch = meta.get("scanner") == "machine_catalog_batch"
                no_sample = not record.get("text_preview")
                if state == "missing":
                    ai_state = "not_requested"
                elif automatic_batch and no_sample:
                    # A signature is not an understanding of a document. Mark
                    # unsupported body extraction for review; media remain L0.
                    if path.suffix.casefold() in IMAGE_SUFFIXES:
                        ai_state = "local_l0"
                        record["tier_basis"] = "signature_only_media"
                    else:
                        ai_state = "unavailable"
                        record["tier_basis"] = "body_sample_unavailable"
                elif (
                    restricted
                    and meta["request"]["provider"] != "local"
                    and not meta["request"].get("allow_restricted_remote_processing")
                ):
                    ai_state = "restricted_local_only"
                    record["tier_basis"] = "restricted_remote_consent_missing"
                else:
                    # Remote quick triage may conclude "metadata only / catalog"
                    # for binary, media, archive, empty, or otherwise non-text
                    # files.  Do not silently count those files as uncovered just
                    # because there is no body sample. Local-only jobs do not
                    # infer an AI decision.
                    ai_state = (
                        "not_requested"
                        if meta["request"]["provider"] == "local"
                        else "pending"
                    )
                with self.db(job) as db:
                    db.execute(
                        "UPDATE files SET state=?,record=?,summary=?,ai_state=? WHERE id=?",
                        (
                            state,
                            dumps(record),
                            dumps(summary),
                            ai_state,
                            row["id"],
                        ),
                    )

    def packet(self, job):
        with self.db(job) as db:
            db.execute("BEGIN IMMEDIATE")
            meta = self.meta(db)
            if meta["stage"] != "agent":
                raise ValueError("当前不在AI处理阶段")
            current = db.execute(
                "SELECT payload FROM packets WHERE state='pending' LIMIT 1"
            ).fetchone()
            if current:
                return json.loads(current[0])
            rows = db.execute(
                "SELECT id,record FROM files WHERE ai_state='pending' AND "
                "state='inspected' ORDER BY id LIMIT ?",
                (meta["request"]["batch_size"],),
            ).fetchall()
            if not rows:
                return None
            items = []
            for row in rows:
                record = json.loads(row["record"])
                items.append(
                    {
                        "id": str(row["id"]),
                        # Keep local placement in the ledger/UI, but do not send
                        # full drive paths or parent names to a remote model.
                        "name": redact_secrets(
                            str(record.get("name") or Path(record["relative"]).name)
                        )[0],
                        "sample": redact_secrets(
                            (record.get("text_preview") or "")[:2000]
                        )[0],
                        "coverage": record.get("coverage", "partial"),
                        "metadata": {
                            "extension": record.get("extension") or record.get("suffix", ""),
                            "detected_type": record.get("detected_type", ""),
                            "bytes": record.get("bytes", 0),
                            "notes": [
                                redact_secrets(str(note))[0]
                                for note in record.get("notes", [])[:5]
                            ]
                            if isinstance(record.get("notes"), list)
                            else [],
                            "sample_available": bool(record.get("text_preview")),
                        },
                        "local_preclassification": {
                            "category_id": record.get("classification", {}).get("category_id"),
                            "label": record.get("classification", {}).get("label"),
                            "basis": record.get("classification", {}).get("basis"),
                            "evidence": [
                                redact_secrets(str(evidence))[0]
                                for evidence in record.get("classification", {}).get(
                                    "evidence", []
                                )
                            ],
                            "candidates": [
                                {
                                    "category_id": candidate.get("category_id"),
                                    "score": candidate.get("score"),
                                    "evidence": [
                                        redact_secrets(str(value))[0]
                                        for value in candidate.get("evidence", [])
                                    ],
                                }
                                for candidate in record.get("classification", {}).get(
                                    "candidates", []
                                )[:3]
                                if isinstance(candidate, dict)
                            ],
                            "confidence": record.get("classification", {}).get("confidence", 0),
                            "review_reason": record.get("classification", {}).get("review_reason"),
                        },
                        "review_instruction": (
                            "请先独立理解sample，再核对本地初步分类是否有误。"
                            "给出主分类、辅助分类、重要性和建议处理深度；"
                            "有正文时必须以sample为证据。"
                            "如果sample为空，只能做低置信度元数据速判："
                            "选择unresolved_other、recommended_mode=catalog、"
                            "evidence为空，并在uncertainty说明未读取正文。"
                        ),
                    }
                )
            packet = {
                "packet_id": uuid.uuid4().hex,
                "items": items,
                "categories": [
                    {"id": c["id"], "name": c["name"]}
                    for c in meta["taxonomy"]["categories"]
                    if c["parent_id"]
                ],
            }
            db.execute(
                "INSERT INTO packets(id,payload,state) VALUES(?,?,'pending')",
                (packet["packet_id"], dumps(packet)),
            )
            return packet

    @staticmethod
    def _validate_agent_result(packet, result, categories):
        source = {item["id"]: item for item in packet["items"]}
        items = result.get("items", [])
        if not isinstance(items, list) or len(items) != len(source):
            raise ValueError("AI回传数量不匹配，未接受本批")
        if {str(i.get("id")) for i in items} != set(source):
            raise ValueError("AI回传文件编号不匹配")
        for item in items:
            ident = str(item["id"])
            # Old user-operated/external integrations may return the prior
            # five-field envelope. New Codex jobs use RESULT_SCHEMA and must
            # return the full understanding profile.
            item.setdefault("purpose", "旧协议未提供用途说明")
            item.setdefault("secondary_category_ids", [])
            item.setdefault("importance", "normal")
            item.setdefault("recommended_mode", "full")
            item.setdefault("recommendation_reason", "旧协议未提供入库建议")
            if item.get("category_id") not in categories:
                raise ValueError("AI使用了未定义分类")
            limits = {
                "summary": 2000,
                "purpose": 300,
                "recommendation_reason": 500,
                "evidence": 40,
                "uncertainty": 1000,
            }
            if any(
                not isinstance(item.get(key), str) or len(item[key]) > limit
                for key, limit in limits.items()
            ):
                raise ValueError("AI字段格式或长度无效")
            secondary = item.get("secondary_category_ids")
            if (
                not isinstance(secondary, list)
                or len(secondary) > 3
                or len(secondary) != len(set(secondary))
                or item["category_id"] in secondary
                or any(category_id not in categories for category_id in secondary)
            ):
                raise ValueError("AI辅助分类无效")
            if item.get("importance") not in {"high", "normal", "low"}:
                raise ValueError("AI重要性无效")
            if item.get("recommended_mode") not in {"catalog", "extract", "full", "semantic"}:
                raise ValueError("AI处理建议无效")
            evidence = item["evidence"]
            if (evidence and evidence not in source[ident]["sample"]) or (
                not evidence and item["category_id"] != "unresolved_other"
            ):
                raise ValueError("AI分类证据不在本批正文中，未接受结果")
            if not source[ident]["sample"] and (
                item["category_id"] != "unresolved_other"
                or item["recommended_mode"] != "catalog"
                or evidence
            ):
                raise ValueError("没有正文样本的文件只能标记为未解析并保留索引")
        return {"items": items}

    def accept(self, job, packet_id, result, thread_id=None):
        with self.db(job) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload,state FROM packets WHERE id=?", (packet_id,)
            ).fetchone()
            if not row:
                raise ValueError("不存在此批次")
            if row["state"] == "done":
                return  # idempotent external delivery
            if row["state"] != "pending":
                raise ValueError("批次已因资料变化失效，请重新获取")
            packet = json.loads(row["payload"])
            meta = self.meta(db)
            categories = {c["id"]: c for c in meta["taxonomy"]["categories"] if c["parent_id"]}
            items = self._validate_agent_result(packet, result, categories)["items"]
            for item in items:
                ident = str(item["id"])
                evidence = item["evidence"]
                stored = db.execute(
                    "SELECT record,summary,path FROM files WHERE id=?", (int(ident),)
                ).fetchone()
                record = json.loads(stored["record"])
                original_summary = json.loads(stored["summary"])
                path = Path(stored["path"])
                try:
                    stat = path.stat()
                    valid = (
                        self._safe(path, self.roots(meta))
                        and stat.st_size == record.get("bytes")
                        and stat.st_mtime_ns == record.get("mtime_ns")
                    )
                except OSError:
                    valid = False
                if not valid:
                    db.execute("UPDATE files SET ai_state='stale' WHERE id=?", (int(ident),))
                    continue
                if original_summary.get("origin") == "user":
                    db.execute("UPDATE files SET ai_state='done' WHERE id=?", (int(ident),))
                    continue  # never overwrite user's confirmed correction
                c = categories[item["category_id"]]
                recommended_mode = item["recommended_mode"]
                model_mode = recommended_mode
                if meta.get("scanner") == "machine_catalog_batch":
                    suffix = path.suffix.casefold()
                    kind = (
                        "markdown" if suffix in MARKDOWN_SUFFIXES else
                        "documents" if suffix in DOCUMENT_SUFFIXES else
                        "images" if suffix in IMAGE_SUFFIXES else "other"
                    )
                    selected_rule = load_auto_policy(self.settings.data_root)["rules"][kind]
                    max_mode = {
                        "catalog": "catalog", "exclude": "catalog",
                        "extract": "extract", "md_fallback": "extract",
                        "md_only": "full", "full": "full", "semantic": "semantic",
                    }[selected_rule]
                    levels = ("catalog", "extract", "full", "semantic")
                    if levels.index(recommended_mode) > levels.index(max_mode):
                        recommended_mode = max_mode
                secondary = [
                    categories[category_id]
                    for category_id in item["secondary_category_ids"]
                ]
                record["classification"] = {
                    "category_id": c["id"],
                    "parent_id": c["parent_id"],
                    "label": c["name"],
                    "basis": "agent_suggestion",
                    "evidence": [evidence],
                    "review_status": "unreviewed",
                    "secondary_category_ids": [category["id"] for category in secondary],
                    "secondary_categories": [
                        {"id": category["id"], "label": category["name"]}
                        for category in secondary
                    ],
                }
                record["understanding"] = {
                    "purpose": redact_secrets(item["purpose"])[0],
                    "importance": item["importance"],
                    "recommended_mode": recommended_mode,
                    "model_recommended_mode": model_mode,
                    "profile_cap_applied": recommended_mode != model_mode,
                    "recommendation_reason": redact_secrets(item["recommendation_reason"])[0],
                    "evidence": redact_secrets(evidence)[0],
                    "uncertainty": redact_secrets(item["uncertainty"])[0],
                    "origin": "agent",
                    "review_status": "unreviewed",
                    "protocol": AI_PROTOCOL,
                }
                summary = {
                    "text": redact_secrets(item["summary"])[0],
                    "purpose": redact_secrets(item["purpose"])[0],
                    "importance": item["importance"],
                    "recommended_mode": recommended_mode,
                    "origin": "agent",
                    "evidence": redact_secrets(evidence)[0],
                    "uncertainty": redact_secrets(item["uncertainty"])[0],
                    "review_status": "unreviewed",
                    "thread_id": thread_id,
                    "packet_id": packet_id,
                }
                db.execute(
                    "UPDATE files SET record=?,summary=?,ai_state='done',revision=revision+1 "
                    "WHERE id=?",
                    (dumps(record), dumps(summary), int(ident)),
                )
            db.execute(
                "UPDATE packets SET state='done',result=?,thread_id=? WHERE id=?",
                (redact_secrets(dumps(result))[0], thread_id, packet_id),
            )
            meta["ai_batches"] += 1
            self.put(db, meta)

    @staticmethod
    def _is_agent_output_validation_error(exc: ValueError) -> bool:
        return any(
            marker in str(exc)
            for marker in (
                "AI回传数量不匹配",
                "AI回传文件编号不匹配",
                "AI使用了未定义分类",
                "AI字段格式或长度无效",
                "AI辅助分类无效",
                "AI重要性无效",
                "AI处理建议无效",
                "AI分类证据不在本批正文中",
                "没有正文样本的文件只能标记为未解析",
            )
        )

    def reject_untrusted_agent_packet(self, job, packet_id, reason):
        """Keep the local result when an agent twice fails output validation.

        This is intentionally a terminal state for this packet: retrying the same
        prompt again would spend more quota without making an unsupported agent
        claim trustworthy.  The original local classification and summary remain
        untouched, and the audit trail stores only the validation reason rather
        than the agent's untrusted response.
        """
        with self.db(job) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload,state FROM packets WHERE id=?", (packet_id,)
            ).fetchone()
            if not row:
                raise ValueError("不存在此批次")
            if row["state"] != "pending":
                return
            packet = json.loads(row["payload"])
            ids = [int(item["id"]) for item in packet["items"]]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE files SET ai_state='rejected' WHERE id IN ({placeholders}) "
                    "AND ai_state='pending'",
                    ids,
                )
            db.execute(
                "UPDATE packets SET state='rejected',result=? WHERE id=?",
                (
                    dumps(
                        {
                            "status": "rejected",
                            "reason": "agent_evidence_not_verifiable",
                            "message": str(reason)[:240],
                        }
                    ),
                    packet_id,
                ),
            )
            meta = self.meta(db)
            meta["ai_rejected_batches"] = int(meta.get("ai_rejected_batches", 0)) + 1
            self.put(db, meta)

    def _deepseek_agent(self, job, stop):
        """Run the same bounded packet protocol without launching Codex."""
        request = self.view(job, limit=0)["request"]
        gateway = DeepSeekGateway(
            settings=self.settings,
            repository=Repository(Database(self.settings)),
        )
        for _ in range(request["max_ai_batches"]):
            if stop.is_set():
                return
            packet = self.packet(job)
            if packet is None:
                self._set(job, stage="markdown")
                return
            with self.db(job) as db:
                meta = self.meta(db)
            categories = {
                c["id"]: c
                for c in meta["taxonomy"]["categories"]
                if c["parent_id"]
            }

            def validate(value, packet=packet, categories=categories):
                return self._validate_agent_result(packet, value, categories)

            try:
                result = gateway.complete_json(
                    task_type="catalog_classification",
                    system_prompt=INSTRUCTIONS,
                    payload=packet,
                    complexity="simple",
                    prompt_version="summary-deepseek-v1",
                    max_tokens=min(1800, max(700, len(packet["items"]) * 300)),
                    use_cache=False,
                    validator=validate,
                    validation_hint=(
                        "每个输入id必须且只能对应一项；evidence必须逐字连续出现在"
                        "对应sample中，否则用unresolved_other和空evidence。"
                    ),
                )
                self.accept(job, packet["packet_id"], result.content)
            except LLMError as exc:
                if exc.code in {
                    "semantic_validation_error",
                    "invalid_json",
                    "invalid_json_shape",
                    "empty_content",
                }:
                    self.reject_untrusted_agent_packet(job, packet["packet_id"], str(exc))
                    continue
                raise AgentError(str(exc)) from exc
        if self.packet(job) is None:
            self._set(job, stage="markdown")
        else:
            self._set(
                job, state="budget_paused", message="本次AI批次数到上限，点击继续可再处理一轮"
            )

    def _agent(self, job, stop):
        with self.db(job) as db:
            meta = self.meta(db)
        if meta["request"]["provider"] == "external":
            if self.packet(job):
                self._set(job, state="awaiting_agent", message="等待用户Agent读取批次并回传JSON")
                return
            self._set(job, stage="markdown")
            return
        if meta["request"]["provider"] == "deepseek":
            self._deepseek_agent(job, stop)
            return
        request = meta["request"]
        cwd = self.directory(job) / "agent-work"
        cwd.mkdir(exist_ok=True)
        with self.db(job) as db:
            last_thread = db.execute(
                "SELECT thread_id FROM packets WHERE thread_id IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        resume_thread_id = str(last_thread[0]) if last_thread else None
        with CodexAgent(
            cwd, request["model"], request["codex_executable"], stop,
            resume_thread_id=resume_thread_id,
        ) as agent:
            for _ in range(request["max_ai_batches"]):
                if stop.is_set():
                    return
                packet = self.packet(job)
                if packet is None:
                    self._set(job, stage="markdown")
                    return

                def save_thread(ident, packet_id=packet["packet_id"]):
                    with self.db(job) as db:
                        db.execute(
                            "UPDATE packets SET thread_id=? WHERE id=?",
                            (ident, packet_id),
                        )

                result, ident = agent.summarize(packet, save_thread)
                try:
                    self.accept(job, packet["packet_id"], result, ident)
                except ValueError as exc:
                    if not self._is_agent_output_validation_error(exc):
                        raise
                    retry_packet = dict(packet)
                    retry_packet["validation_feedback"] = AGENT_RETRY_VALIDATION_FEEDBACK
                    result, ident = agent.summarize(retry_packet, save_thread)
                    try:
                        self.accept(job, packet["packet_id"], result, ident)
                    except ValueError as retry_exc:
                        if not self._is_agent_output_validation_error(retry_exc):
                            raise
                        self.reject_untrusted_agent_packet(
                            job, packet["packet_id"], str(retry_exc)
                        )
                # One durable, constrained thread per summary job.  The input remains a
                # one-file packet and each result is locally validated, so reopening a
                # thread every ten packets only pollutes the user's Codex history.
        if self.packet(job) is None:
            self._set(job, stage="markdown")
        else:
            self._set(
                job, state="budget_paused", message="本次AI批次数到上限，点击继续可再处理一轮"
            )

    def _run(self, job, stop, lock, started, finished):
        started.set()
        try:
            self._refresh_changed(job, stop)
            with self.db(job) as db:
                stage = self.meta(db)["stage"]
            if stage == "discover":
                self._discover(job, stop)
            with self.db(job) as db:
                stage = self.meta(db)["stage"]
            if stage == "inspect" and not stop.is_set():
                self._inspect(job, stop)
            with self.db(job) as db:
                stage = self.meta(db)["stage"]
            if stage == "agent" and not stop.is_set():
                self._agent(job, stop)
            with self.db(job) as db:
                stage = self.meta(db)["stage"]
            if stage == "markdown" and not stop.is_set():
                target = self.directory(job) / ("md-" + uuid.uuid4().hex[:8])
                self._markdown(job, target, stop)
                if not stop.is_set():
                    state = self.view(job, limit=0)
                    warning = (
                        state["counts"].get("error", 0)
                        or state["directory_errors"]
                        or state["ai_counts"].get("stale", 0)
                        or state["ai_counts"].get("unavailable", 0)
                        or state["ai_counts"].get("rejected", 0)
                    )
                    with self.db(job) as db:
                        meta = self.meta(db)
                        ledger = None
                        if meta.get("scanner") == "machine_catalog_batch":
                            ledger = self.catalog_ledger.record_job(
                                job, self.directory(job) / "queue.sqlite"
                            )
                        meta.update(
                            stage="done",
                            state="warning" if warning else "completed",
                            md_path=str(target),
                            message="MD已生成；抽样不代表读完全文，AI结论待核对",
                            classification_counts=self._classification_counts(db, meta),
                        )
                        if ledger:
                            meta["catalog_ledger"] = ledger
                        self.put(db, meta)
        except (AgentError, ValueError) as exc:
            self._set(job, state="warning", message=str(exc))
        except Exception:
            self._set(job, state="warning", message="处理异常，进度已保留；未完成部分可继续")
        finally:
            if stop.is_set():
                self._set(job, state="paused", message="已暂停，继续时复用已完成的检查记录")
            lock.release()
            finished.set()

    def _markdown(self, job, target, stop=None):
        target.mkdir(parents=True, exist_ok=False)
        offset = 0
        while not stop or not stop.is_set():
            data = self.view(job, offset, 100)
            if not data["records"]:
                break
            lines = [
                "# 文件整理记录",
                "",
                f"来源目录：{data['root']}",
                f"任务：{job}",
                "",
                "仅基于内容抽样。AI建议未经人工确认，不证明任务完成。",
                "",
            ]
            for item in data["records"]:
                record, summary = item["record"] or {}, item["summary"] or {}
                understanding = record.get("understanding") or {}
                title = record.get("relative", str(item["id"]))
                lines += [
                    f"## 文件 {item['id']}",
                    "",
                    f"路径：{markdown_literal(title)}",
                    f"分类：{record.get('classification', {}).get('label', '待判断')}",
                    f"检查范围：{record.get('coverage', '未读取')}；状态：{item['state']}",
                    f"检查时间：{record.get('inspected_at', '未读取')}",
                    f"来源标识：{record.get('prefix_sha256', '无')}（前缀，不是全文hash）",
                    f"摘要来源：{summary.get('origin', '无')}；AI状态：{item['ai_state']}",
                    "",
                    markdown_literal(summary.get("text", "暂无摘要")),
                    "",
                    "AI用途：" + markdown_literal(understanding.get("purpose", "尚未由AI理解")),
                    "AI建议处理深度："
                    + markdown_literal(understanding.get("recommended_mode", "无")),
                    "建议理由："
                    + markdown_literal(understanding.get("recommendation_reason", "无")),
                    "",
                    "不确定项："
                    + markdown_literal(summary.get("uncertainty", "仅完成抽样，未全文理解")),
                    "",
                    "### 正文样本",
                    "",
                ]
                excerpt = record.get("text_preview") or "没有可用文本样本"
                lines += ["> " + markdown_literal(line) for line in excerpt[:2000].splitlines()]
                lines += [""]
            # Neutralize raw HTML from hostile document excerpts; no executable Markdown HTML.
            text = "\n".join(lines)
            atomic_write(target / f"资料-{offset // 100 + 1:05d}.md", text.encode("utf-8"))
            offset += len(data["records"])
        atomic_write(
            target / "README.md",
            (
                f"# 整理任务 {job}\n\n包含 {offset} 条逐文件记录。\n"
                "原件没有复制或移动。分类可修改，AI信息只作为待核对的上下文。\n"
            ).encode(),
        )

    def edit(self, job, ident, edit):
        if self.view(job, limit=0)["running"]:
            raise ValueError("请先暂停再修改，避免与AI结果冲突")
        with self.db(job) as db:
            db.execute("BEGIN IMMEDIATE")
            meta = self.meta(db)
            category = next(
                (
                    c
                    for c in load_taxonomy(self.settings.data_root)["categories"]
                    if c["id"] == edit.category_id and c["parent_id"]
                ),
                None,
            )
            row = db.execute("SELECT * FROM files WHERE id=?", (ident,)).fetchone()
            if not row or not row["record"] or row["revision"] != edit.revision or not category:
                raise ValueError("文件状态或分类已改变，请刷新")
            previous = dumps(dict(row)).encode("utf-8")
            backup = self.directory(job) / f"edit-{ident}.previous.json"
            atomic_write(backup, previous)
            if backup.read_bytes() != previous:
                raise OSError("修改恢复点验证失败")
            record = json.loads(row["record"])
            record["classification"] = {
                "category_id": category["id"],
                "parent_id": category["parent_id"],
                "label": category["name"],
                "basis": "user_choice",
                "review_status": "confirmed",
                "secondary_category_ids": [],
                "secondary_categories": [],
            }
            if record.get("understanding"):
                record["understanding"]["review_status"] = "user_overrode_primary_category"
            db.execute(
                "UPDATE files SET record=?,summary=?,revision=revision+1 WHERE id=?",
                (
                    dumps(record),
                    dumps(
                        {
                            "text": redact_secrets(edit.summary)[0],
                            "origin": "user",
                            "review_status": "confirmed",
                        }
                    ),
                    ident,
                ),
            )
            if meta["stage"] == "done":
                meta["classification_counts"] = self._classification_counts(db, meta)
            meta["message"] = "修改已保存；已导出的MD不会被覆盖，再次导出可生成新版"
            self.put(db, meta)

    def export(self, job, destination):
        data = self.view(job, limit=0)
        if data["running"] or data["stage"] != "done":
            raise ValueError("请在整理完成后导出")
        target = authorize_root(Path(destination))
        roots = self.roots(data)
        if data.get("scope") != "all_local_drives" and any(
            target == root or root in target.parents for root in roots
        ):
            raise ValueError("请选择源范围以外的输出目录，防止摘要再次被扫描")
        output = target / ("知枢整理-" + job[:8] + "-" + uuid.uuid4().hex[:8])
        self._markdown(job, output)
        return {"path": str(output), "source_files_written": 0}
