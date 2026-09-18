"""Separate, resumable machine file catalog (A index); never copies source files."""

from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pkas.config import Settings
from pkas.everything_scanner import executable as everything_executable
from pkas.everything_scanner import status as everything_status
from pkas.ingest import SKIP_DIRECTORIES, is_sensitive_path
from pkas.intake import category


class MachineCatalogStart(BaseModel):
    confirmed: bool = False


SYSTEM_DIRECTORIES = {
    "$recycle.bin",
    "system volume information",
    "recovery",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    "msocache",
} | SKIP_DIRECTORIES


def now() -> str:
    return datetime.now(UTC).isoformat()


def norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path))).rstrip("\\/")


def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def safe_slug(value: str) -> str:
    clean = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value, flags=re.UNICODE).strip("-.")
    return (clean[:24] or "root") + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def windows_known_folders() -> list[Path]:
    folders = [Path.home() / name for name in ("Desktop", "Documents", "Downloads")]
    if os.name != "nt":
        return folders
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            values = ("Desktop", "Personal", "{374DE290-123F-4565-9164-39C4925E467B}")
            folders = [
                Path(os.path.expandvars(winreg.QueryValueEx(key, value)[0])) for value in values
            ]
    except OSError:
        pass
    return list(dict.fromkeys(folder for folder in folders if folder.is_dir()))


def eligible_scopes() -> list[dict[str, str]]:
    """C is conservative; every other local fixed drive is cataloged from its root."""
    if os.name != "nt":
        return [
            {"path": str(folder), "kind": "safe_user_folder"} for folder in windows_known_folders()
        ]
    data_drives: list[Path] = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(str(root))) == 3:
            data_drives.append(root)
    scopes = [{"path": str(root), "kind": "fixed_data_drive"} for root in data_drives]
    data_letters = {root.drive.casefold() for root in data_drives}
    for folder in windows_known_folders():
        if folder.drive.casefold() == "c:" or folder.drive.casefold() not in data_letters:
            scopes.append({"path": str(folder), "kind": "safe_user_folder"})
    return scopes


class MachineCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.home = settings.data_root / "machine-catalog"
        self.path = self.home / "catalog.sqlite"
        self.summary_home = settings.data_root / "derived" / "machine-catalog"
        self.home.mkdir(parents=True, exist_ok=True)
        self.summary_home.mkdir(parents=True, exist_ok=True)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._excluded_roots = self._build_exact_exclusions()
        self.initialize()
        self._recover_interrupted()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @property
    def status_path(self) -> Path:
        return self.home / "status.json"

    def write_cached_status(
        self, job_id: str, *, catalog_files: int, represented_bytes: int
    ) -> None:
        payload = {
            "job_id": job_id,
            "catalog_files": int(catalog_files),
            "represented_bytes": int(represented_bytes),
            "updated_at": now(),
        }
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.status_path)

    def read_cached_status(self, job_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if payload.get("job_id") == job_id else {}

    def initialize(self) -> None:
        with self.connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, stage TEXT NOT NULL,
                    started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
                    files_seen INTEGER NOT NULL DEFAULT 0,
                    directories_seen INTEGER NOT NULL DEFAULT 0,
                    excluded INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0, summary_files INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '', owner_pid INTEGER
                );
                CREATE TABLE IF NOT EXISTS scopes(
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    root_path TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    files_seen INTEGER NOT NULL DEFAULT 0,
                    directories_seen INTEGER NOT NULL DEFAULT 0,
                    excluded INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(job_id, root_path)
                );
                CREATE TABLE IF NOT EXISTS directory_queue(
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    scope_id TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error_code TEXT,
                    PRIMARY KEY(job_id, path)
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_queue ON directory_queue(job_id,state,path);
                CREATE TABLE IF NOT EXISTS files(
                    id INTEGER PRIMARY KEY, scope_path TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL, parent_path TEXT NOT NULL, top_group TEXT NOT NULL,
                    name TEXT NOT NULL, extension TEXT NOT NULL, category TEXT NOT NULL,
                    byte_size INTEGER NOT NULL, modified_ns INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active', last_job_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_scope_state ON files(scope_path,state);
                CREATE INDEX IF NOT EXISTS idx_catalog_parent ON files(scope_path,parent_path);
                CREATE INDEX IF NOT EXISTS idx_catalog_group
                ON files(scope_path,top_group,state);
                CREATE INDEX IF NOT EXISTS idx_catalog_kind ON files(category,extension);
                CREATE INDEX IF NOT EXISTS idx_catalog_extension_state ON files(extension,state);
                CREATE INDEX IF NOT EXISTS idx_catalog_modified ON files(modified_ns DESC);
                CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                    name, relative_path, extension, category,
                    content='files', content_rowid='id', tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                    INSERT INTO files_fts(rowid,name,relative_path,extension,category)
                    VALUES(new.id,new.name,new.relative_path,new.extension,new.category);
                END;
                CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                    INSERT INTO files_fts(files_fts,rowid,name,relative_path,extension,category)
                    VALUES('delete',old.id,old.name,old.relative_path,old.extension,old.category);
                END;
                DROP TRIGGER IF EXISTS files_au;
                CREATE TRIGGER files_au AFTER UPDATE OF name,relative_path,extension,category
                ON files WHEN old.name<>new.name OR old.relative_path<>new.relative_path
                    OR old.extension<>new.extension OR old.category<>new.category BEGIN
                    INSERT INTO files_fts(files_fts,rowid,name,relative_path,extension,category)
                    VALUES('delete',old.id,old.name,old.relative_path,old.extension,old.category);
                    INSERT INTO files_fts(rowid,name,relative_path,extension,category)
                    VALUES(new.id,new.name,new.relative_path,new.extension,new.category);
                END;
                """
            )
            columns = {row[1] for row in c.execute("PRAGMA table_info(jobs)")}
            if "owner_pid" not in columns:
                c.execute("ALTER TABLE jobs ADD COLUMN owner_pid INTEGER")

    def _recover_interrupted(self) -> None:
        with self.connect() as c:
            interrupted = [
                row["id"]
                for row in c.execute(
                    "SELECT id,owner_pid FROM jobs WHERE state='running'"
                ).fetchall()
                if not process_alive(row["owner_pid"])
            ]
            for job_id in interrupted:
                c.execute(
                    "UPDATE directory_queue SET state='pending' WHERE job_id=? AND state='working'",
                    (job_id,),
                )
                c.execute(
                    "UPDATE jobs SET state='paused',owner_pid=NULL,"
                    "message='应用曾退出；进度已保留，可继续' WHERE id=?",
                    (job_id,),
                )
            c.commit()

    def _build_exact_exclusions(self) -> list[str]:
        candidates = [
            self.settings.project_root,
            Path("G:/PKAS-backups"),
            Path("G:/PKAS-test-runs"),
            Path("I:/PKAS-backups"),
            Path("I:/PKAS-test-runs"),
        ]
        return [norm(path) for path in candidates if path.exists()]

    def _excluded(self, path: Path, *, directory: bool = False) -> bool:
        if directory and path.name.casefold() in SYSTEM_DIRECTORIES:
            return True
        if is_sensitive_path(path):
            return True
        normalized = norm(path)
        for blocked in self._excluded_roots:
            if normalized == blocked or normalized.startswith(blocked + os.sep):
                return True
        return False

    def start(self, *, confirmed: bool, scopes: list[dict[str, str]] | None = None) -> dict:
        if not confirmed:
            raise ValueError("全机目录索引需要明确确认")
        current = self.latest()
        if current and current["state"] == "running":
            return current
        selected = scopes or eligible_scopes()
        selected = [item for item in selected if Path(item["path"]).is_dir()]
        if not selected:
            raise ValueError("没有找到可扫描的本地范围")
        job_id, timestamp = "mcat_" + uuid.uuid4().hex, now()
        with self.connect() as c:
            c.execute(
                "INSERT INTO jobs(id,state,stage,started_at,updated_at,message,owner_pid) "
                "VALUES(?, 'running', 'catalog', ?, ?, '正在建立A库文件目录索引', ?)",
                (job_id, timestamp, timestamp, os.getpid()),
            )
            for item in selected:
                root = str(Path(item["path"]).resolve())
                scope_id = "scope_" + hashlib.sha256(root.casefold().encode()).hexdigest()[:20]
                c.execute(
                    "INSERT INTO scopes(id,job_id,root_path,kind) VALUES(?,?,?,?)",
                    (scope_id, job_id, root, item["kind"]),
                )
                c.execute(
                    "INSERT INTO directory_queue(job_id,scope_id,path) VALUES(?,?,?)",
                    (job_id, scope_id, root),
                )
            c.commit()
        self.resume(job_id)
        return self.view(job_id)

    def resume(self, job_id: str) -> dict:
        if self._thread and self._thread.is_alive():
            return self.view(job_id)
        with self.connect() as c:
            row = c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("全机索引任务不存在")
            if row["state"] == "completed":
                return self.view(job_id)
            c.execute(
                "UPDATE jobs SET state='running',updated_at=?,"
                "message='正在继续建立A库索引',owner_pid=? WHERE id=?",
                (now(), os.getpid(), job_id),
            )
            c.commit()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(job_id,), daemon=True)
        self._thread.start()
        return self.view(job_id)

    def pause(self, job_id: str) -> dict:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        with self.connect() as c:
            c.execute(
                "UPDATE jobs SET state='paused',updated_at=?,"
                "message='已暂停；目录队列和文件进度已保留' "
                "owner_pid=NULL WHERE id=? AND state<>'completed'",
                (now(), job_id),
            )
            c.commit()
        return self.view(job_id)

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    @staticmethod
    def _top_group(relative: str) -> str:
        parts = Path(relative).parts
        return parts[0] if len(parts) > 1 else "_root"

    def _scan_directory(self, c: sqlite3.Connection, job_id: str, row: sqlite3.Row) -> None:
        directory = Path(row["path"])
        scope = c.execute("SELECT * FROM scopes WHERE id=?", (row["scope_id"],)).fetchone()
        if not scope:
            return
        root = Path(scope["root_path"])
        files: list[tuple[Any, ...]] = []
        children: list[str] = []
        excluded = errors = 0
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            code = getattr(exc, "winerror", None) or getattr(exc, "errno", None) or "unknown"
            c.execute(
                "UPDATE directory_queue SET state='error',error_code=? WHERE job_id=? AND path=?",
                (f"os_error_{code}", job_id, str(directory)),
            )
            c.execute("UPDATE jobs SET errors=errors+1,updated_at=? WHERE id=?", (now(), job_id))
            c.execute("UPDATE scopes SET errors=errors+1 WHERE id=?", (scope["id"],))
            c.commit()
            return
        with entries:
            for entry in entries:
                path = Path(entry.path)
                try:
                    stat = entry.stat(follow_symlinks=False)
                    reparse = bool(getattr(stat, "st_file_attributes", 0) & 0x400)
                    if entry.is_dir(follow_symlinks=False):
                        if reparse or self._excluded(path, directory=True):
                            excluded += 1
                        else:
                            children.append(str(path))
                        continue
                    if not entry.is_file(follow_symlinks=False) or reparse or self._excluded(path):
                        excluded += 1
                        continue
                    relative = str(path.relative_to(root))
                    timestamp = now()
                    files.append(
                        (
                            str(root),
                            str(path),
                            relative,
                            str(path.parent),
                            self._top_group(relative),
                            path.name,
                            path.suffix.casefold(),
                            category(path),
                            int(stat.st_size),
                            int(stat.st_mtime_ns),
                            job_id,
                            timestamp,
                            timestamp,
                        )
                    )
                except (OSError, ValueError):
                    errors += 1
        c.executemany(
            """INSERT INTO files(
                scope_path,path,relative_path,parent_path,top_group,name,extension,category,
                byte_size,modified_ns,last_job_id,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                scope_path=excluded.scope_path,relative_path=excluded.relative_path,
                parent_path=excluded.parent_path,top_group=excluded.top_group,name=excluded.name,
                extension=excluded.extension,category=excluded.category,byte_size=excluded.byte_size,
                modified_ns=excluded.modified_ns,state='active',last_job_id=excluded.last_job_id,
                last_seen_at=excluded.last_seen_at""",
            files,
        )
        c.executemany(
            "INSERT OR IGNORE INTO directory_queue(job_id,scope_id,path) VALUES(?,?,?)",
            [(job_id, scope["id"], child) for child in children],
        )
        c.execute(
            "UPDATE directory_queue SET state='done',error_code=NULL WHERE job_id=? AND path=?",
            (job_id, str(directory)),
        )
        c.execute(
            "UPDATE jobs SET files_seen=files_seen+?,directories_seen=directories_seen+1,"
            "excluded=excluded+?,errors=errors+?,updated_at=? WHERE id=?",
            (len(files), excluded, errors, now(), job_id),
        )
        c.execute(
            "UPDATE scopes SET files_seen=files_seen+?,directories_seen=directories_seen+1,"
            "excluded=excluded+?,errors=errors+? WHERE id=?",
            (len(files), excluded, errors, scope["id"]),
        )

    @staticmethod
    def _filetime_ns(value: str) -> int:
        try:
            raw = int(value or 0)
        except ValueError:
            return 0
        return max(0, (raw - 116_444_736_000_000_000) * 100)

    def _fast_upsert(
        self,
        c: sqlite3.Connection,
        job_id: str,
        scope: sqlite3.Row,
        rows: list[tuple[Any, ...]],
        directories: int,
        excluded: int,
        errors: int,
    ) -> None:
        c.executemany(
            """INSERT INTO files(
                scope_path,path,relative_path,parent_path,top_group,name,extension,category,
                byte_size,modified_ns,last_job_id,first_seen_at,last_seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                scope_path=excluded.scope_path,relative_path=excluded.relative_path,
                parent_path=excluded.parent_path,top_group=excluded.top_group,name=excluded.name,
                extension=excluded.extension,category=excluded.category,byte_size=excluded.byte_size,
                modified_ns=excluded.modified_ns,state='active',last_job_id=excluded.last_job_id,
                last_seen_at=excluded.last_seen_at""",
            rows,
        )
        c.execute(
            "UPDATE scopes SET files_seen=files_seen+?,directories_seen=directories_seen+?,"
            "excluded=excluded+?,errors=errors+? WHERE id=?",
            (len(rows), directories, excluded, errors, scope["id"]),
        )
        c.execute(
            """UPDATE jobs SET
                files_seen=(SELECT COALESCE(SUM(files_seen),0) FROM scopes WHERE job_id=?),
                directories_seen=(
                    SELECT COALESCE(SUM(directories_seen),0) FROM scopes WHERE job_id=?
                ),
                excluded=(SELECT COALESCE(SUM(excluded),0) FROM scopes WHERE job_id=?),
                errors=(SELECT COALESCE(SUM(errors),0) FROM scopes WHERE job_id=?),
                updated_at=? WHERE id=?""",
            (job_id, job_id, job_id, job_id, now(), job_id),
        )
        c.commit()

    def _everything_scope(self, c: sqlite3.Connection, job_id: str, scope: sqlite3.Row) -> None:
        root = Path(scope["root_path"])
        output = self.home / f"{job_id}-{scope['id']}.efu"
        output.unlink(missing_ok=True)
        excluded_names = ";".join(sorted(SYSTEM_DIRECTORIES))
        process = subprocess.Popen(
            [
                str(everything_executable(self.settings.project_root)),
                "-create-file-list",
                str(output),
                str(root),
                "-create-file-list-exclude-folders",
                excluded_names,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        started = time.monotonic()
        while process.poll() is None:
            if self._stop.wait(0.2):
                process.terminate()
                process.wait(timeout=10)
                raise RuntimeError("scan_paused")
            if time.monotonic() - started > 1800:
                process.terminate()
                process.wait(timeout=10)
                raise RuntimeError("everything_timeout")
        if process.returncode != 0 or not output.is_file():
            raise RuntimeError("everything_failed")

        c.execute(
            "UPDATE scopes SET files_seen=0,directories_seen=0,excluded=0,errors=0 WHERE id=?",
            (scope["id"],),
        )
        c.commit()
        batch: list[tuple[Any, ...]] = []
        directories = excluded = errors = 0
        timestamp = now()
        root_prefix = norm(root) + os.sep
        with output.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or "Filename" not in reader.fieldnames:
                raise RuntimeError("everything_invalid_file_list")
            for raw in reader:
                if self._stop.is_set():
                    raise RuntimeError("scan_paused")
                try:
                    path = Path(raw["Filename"])
                    attributes = int(raw.get("Attributes") or 0)
                    if attributes & 0x10:
                        directories += 1
                        continue
                    if attributes & 0x400 or not norm(path).startswith(root_prefix):
                        excluded += 1
                        continue
                    if self._excluded(path):
                        excluded += 1
                        continue
                    relative = str(path.relative_to(root))
                    batch.append(
                        (
                            str(root),
                            str(path),
                            relative,
                            str(path.parent),
                            self._top_group(relative),
                            path.name,
                            path.suffix.casefold(),
                            category(path),
                            max(0, int(raw.get("Size") or 0)),
                            self._filetime_ns(raw.get("Date Modified") or "0"),
                            job_id,
                            timestamp,
                            timestamp,
                        )
                    )
                except (OSError, ValueError):
                    errors += 1
                if len(batch) >= 5000:
                    self._fast_upsert(c, job_id, scope, batch, directories, excluded, errors)
                    batch, directories, excluded, errors = [], 0, 0, 0
        self._fast_upsert(c, job_id, scope, batch, directories, excluded, errors)
        c.execute("UPDATE scopes SET state='done' WHERE id=?", (scope["id"],))
        c.execute("DELETE FROM directory_queue WHERE scope_id=?", (scope["id"],))
        c.commit()
        output.unlink(missing_ok=True)

    def _fast_scopes(self, c: sqlite3.Connection, job_id: str) -> None:
        if not everything_status(self.settings.project_root)["available"]:
            return
        scopes = c.execute(
            "SELECT * FROM scopes WHERE job_id=? AND state<>'done' ORDER BY root_path",
            (job_id,),
        ).fetchall()
        for scope in scopes:
            if self._stop.is_set():
                return
            c.execute(
                "UPDATE jobs SET stage='catalog_everything',message=?,updated_at=? WHERE id=?",
                (f"Everything正在生成并导入 {scope['root_path']} 清单", now(), job_id),
            )
            c.commit()
            try:
                self._everything_scope(c, job_id, scope)
            except RuntimeError as exc:
                if str(exc) == "scan_paused":
                    return
                c.execute("DELETE FROM directory_queue WHERE scope_id=?", (scope["id"],))
                c.execute(
                    "INSERT OR IGNORE INTO directory_queue(job_id,scope_id,path) VALUES(?,?,?)",
                    (job_id, scope["id"], scope["root_path"]),
                )
                c.execute(
                    "UPDATE scopes SET state='fallback',errors=errors+1 WHERE id=?",
                    (scope["id"],),
                )
                c.commit()

    def _run(self, job_id: str) -> None:
        try:
            with self.connect() as c:
                self._fast_scopes(c, job_id)
                uncommitted_directories = 0
                while not self._stop.is_set():
                    row = c.execute(
                        "SELECT * FROM directory_queue WHERE job_id=? AND state='pending' "
                        "ORDER BY rowid LIMIT 1",
                        (job_id,),
                    ).fetchone()
                    if not row:
                        break
                    c.execute(
                        "UPDATE directory_queue SET state='working' WHERE job_id=? AND path=?",
                        (job_id, row["path"]),
                    )
                    self._scan_directory(c, job_id, row)
                    uncommitted_directories += 1
                    if uncommitted_directories >= 100:
                        c.commit()
                        uncommitted_directories = 0
                c.commit()
                if self._stop.is_set():
                    c.execute("UPDATE directory_queue SET state='pending' WHERE state='working'")
                    c.execute(
                        "UPDATE jobs SET state='paused',updated_at=?,"
                        "message='已暂停；进度已保留',owner_pid=NULL WHERE id=?",
                        (now(), job_id),
                    )
                    c.commit()
                    return
                c.execute(
                    "UPDATE jobs SET stage='summaries',updated_at=? WHERE id=?", (now(), job_id)
                )
                c.commit()
            count = self.generate_summaries(job_id)
            with self.connect() as c:
                timestamp = now()
                c.execute(
                    "UPDATE files SET state='missing' WHERE scope_path IN "
                    "(SELECT root_path FROM scopes WHERE job_id=?) AND last_job_id<>?",
                    (job_id, job_id),
                )
                c.execute("UPDATE scopes SET state='done' WHERE job_id=?", (job_id,))
                c.execute(
                    "UPDATE jobs SET state='completed',stage='done',summary_files=?,updated_at=?,"
                    "finished_at=?,message='A库全盘索引和目录MD已完成',owner_pid=NULL "
                    "WHERE id=?",
                    (count, timestamp, timestamp, job_id),
                )
                c.commit()
                active = c.execute("SELECT COUNT(*) FROM files WHERE state='active'").fetchone()[0]
                represented = c.execute(
                    "SELECT COALESCE(SUM(byte_size),0) FROM files WHERE state='active'"
                ).fetchone()[0]
            self.write_cached_status(
                job_id, catalog_files=int(active), represented_bytes=int(represented)
            )
        except Exception:
            with self.connect() as c:
                c.execute("UPDATE directory_queue SET state='pending' WHERE state='working'")
                c.execute(
                    "UPDATE jobs SET state='warning',updated_at=?,"
                    "message='索引异常；进度已保留，可继续',"
                    "owner_pid=NULL WHERE id=?",
                    (now(), job_id),
                )
                c.commit()

    def generate_summaries(self, job_id: str) -> int:
        target = self.summary_home / job_id
        target.mkdir(parents=True, exist_ok=True)
        written = 0
        with self.connect() as c:
            scopes = c.execute(
                "SELECT * FROM scopes WHERE job_id=? ORDER BY root_path", (job_id,)
            ).fetchall()
            for scope in scopes:
                groups = c.execute(
                    "SELECT top_group,COUNT(*) AS n,SUM(byte_size) AS bytes,"
                    "MAX(modified_ns) AS newest FROM files WHERE scope_path=? AND state='active' "
                    "GROUP BY top_group ORDER BY top_group",
                    (scope["root_path"],),
                ).fetchall()
                drive_dir = target / safe_slug(scope["root_path"])
                drive_dir.mkdir(parents=True, exist_ok=True)
                for group in groups:
                    kinds = c.execute(
                        "SELECT category,COUNT(*) AS n FROM files "
                        "WHERE scope_path=? AND top_group=? "
                        "AND state='active' GROUP BY category ORDER BY n DESC",
                        (scope["root_path"], group["top_group"]),
                    ).fetchall()
                    examples = c.execute(
                        "SELECT relative_path,category,byte_size,modified_ns FROM files "
                        "WHERE scope_path=? AND top_group=? AND state='active' "
                        "ORDER BY modified_ns DESC LIMIT 40",
                        (scope["root_path"], group["top_group"]),
                    ).fetchall()
                    lines = [
                        f"# 目录摘要 · {group['top_group']}",
                        "",
                        f"- 来源范围：`{scope['root_path']}`",
                        f"- 生成时间：{now()}",
                        f"- 文件数：{group['n']:,}",
                        f"- 总大小：{int(group['bytes'] or 0):,} 字节",
                        "",
                        "## 类型分布",
                        "",
                    ]
                    lines.extend(f"- {item['category']}：{item['n']:,}" for item in kinds)
                    lines += ["", "## 最近变更样本", ""]
                    lines.extend(
                        f"- `{item['relative_path']}` · {item['category']} · "
                        f"{item['byte_size']:,} B"
                        for item in examples
                    )
                    lines += [
                        "",
                        "## 说明",
                        "",
                        "本文件由A库目录索引确定性生成，只概括目录结构和文件元数据；",
                        "没有读取正文、没有调用云模型，也不代表图片已经OCR。每个文件的完整记录保存在A库。",
                    ]
                    output = drive_dir / (safe_slug(group["top_group"]) + ".md")
                    temporary = output.with_suffix(".tmp")
                    temporary.write_text("\n".join(lines), encoding="utf-8")
                    os.replace(temporary, output)
                    written += 1
        return written

    def latest(self) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT id FROM jobs ORDER BY started_at DESC LIMIT 1").fetchone()
        return self.view(row["id"]) if row else None

    def view(self, job_id: str) -> dict:
        with self.connect() as c:
            job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise ValueError("全机索引任务不存在")
            queue = dict(
                c.execute(
                    "SELECT state,COUNT(*) FROM directory_queue WHERE job_id=? GROUP BY state",
                    (job_id,),
                ).fetchall()
            )
            scopes = [
                dict(row)
                for row in c.execute(
                    "SELECT root_path,kind,state,files_seen,directories_seen,excluded,errors "
                    "FROM scopes WHERE job_id=? ORDER BY root_path",
                    (job_id,),
                ).fetchall()
            ]
        data = dict(job)
        cached = self.read_cached_status(job_id)
        data.update(
            queue=queue,
            scopes=scopes,
            catalog_files=int(cached.get("catalog_files") or data["files_seen"]),
            represented_bytes=int(cached.get("represented_bytes") or 0),
            database_bytes=self.path.stat().st_size if self.path.exists() else 0,
            summary_path=str(self.summary_home / job_id),
            running=bool(data["state"] == "running" and process_alive(data["owner_pid"])),
            architecture={
                "A": "SQLite文件目录索引（路径、类型、大小、时间）",
                "B": "Qdrant正文向量索引",
                "AB": "A精确命中 + 正文FTS + B语义向量融合",
                "images": "本阶段仅登记元数据，OCR/视觉切分延期",
            },
        )
        return data

    def search(self, query: str, limit: int = 40) -> list[dict[str, Any]]:
        value = query.strip()
        if not value:
            return []
        limit = max(1, min(limit, 200))
        with self.connect() as c:
            if len(value) >= 3:
                escaped = '"' + value.replace('"', '""') + '"'
                rows = c.execute(
                    "SELECT f.*,bm25(files_fts) AS score FROM files_fts "
                    "JOIN files f ON f.id=files_fts.rowid WHERE files_fts MATCH ? "
                    "AND f.state='active' ORDER BY score LIMIT ?",
                    (escaped, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT *,0.0 AS score FROM files WHERE state='active' "
                    "AND (name LIKE ? OR relative_path LIKE ?) ORDER BY modified_ns DESC LIMIT ?",
                    (f"%{value}%", f"%{value}%", limit),
                ).fetchall()
        return [
            {
                "source_id": f"catalog:{row['id']}",
                "document_id": None,
                "chunk_id": None,
                "title": row["name"],
                "snippet": f"{row['relative_path']} · {row['category']} · {row['byte_size']:,} B",
                "locator": row["path"],
                "original_uri": row["path"],
                "source_type": "filesystem-catalog",
                "domain": "local",
                "privacy": "private",
                "retrieval_channels": ["catalog"],
                "catalog_score": float(row["score"]),
                "modified_ns": int(row["modified_ns"]),
            }
            for row in rows
        ]

    def integrity(self) -> dict[str, Any]:
        with self.connect() as c:
            quick = c.execute("PRAGMA quick_check").fetchone()[0]
            files = c.execute("SELECT COUNT(*) FROM files WHERE state='active'").fetchone()[0]
            fts = c.execute("SELECT COUNT(*) FROM files_fts").fetchone()[0]
            images = c.execute(
                "SELECT COUNT(*) FROM files WHERE state='active' AND category='images'"
            ).fetchone()[0]
        return {"quick_check": quick, "files": files, "fts_rows": fts, "images_deferred": images}
