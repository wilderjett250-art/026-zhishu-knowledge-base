import hashlib
import json
import os
from pathlib import Path
from typing import Any

from pkas.codex_capture import build_codex_turn, is_internal_codex_turn, redact_secrets
from pkas.config import Settings, get_settings
from pkas.db import Database
from pkas.ingest import SKIP_DIRECTORIES, IngestionService, is_sensitive_path
from pkas.parsers import SUPPORTED_EXTENSIONS, ParseError
from pkas.repository import Repository, new_id, utc_now

CONNECTOR_TYPES = {"local_files", "codex_sessions"}
SYNC_MODES = {"catalog", "index"}
DOMAINS = {"work", "self", "shared", "distill"}
PRIVACY_LEVELS = {"public", "private", "restricted"}


class SyncBoundaryError(ValueError):
    pass


class SyncService:
    def __init__(
        self,
        settings: Settings | None = None,
        database: Database | None = None,
        repository: Repository | None = None,
        ingestion: IngestionService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.database = database or Database(self.settings)
        self.repository = repository or Repository(self.database)
        self.ingestion = ingestion or IngestionService(self.settings, self.repository)
        self.database.initialize()

    def _validate_root(self, raw_path: str) -> Path:
        root = Path(raw_path).expanduser()
        if not root.is_absolute():
            raise SyncBoundaryError("资料源路径必须是绝对路径。")
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise SyncBoundaryError("资料源必须是一个现存目录。")
        if resolved == Path(resolved.anchor):
            raise SyncBoundaryError("不允许把整块磁盘根目录注册为资料源。")
        project_root = self.settings.project_root.resolve()
        if resolved == project_root or project_root in resolved.parents:
            raise SyncBoundaryError("知识库项目目录不能注册为外部资料源。")
        return resolved

    def register_root(
        self,
        *,
        name: str,
        root_path: str,
        connector_type: str,
        domain: str = "work",
        privacy: str = "private",
        sync_mode: str = "catalog",
        recursive: bool = True,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if connector_type not in CONNECTOR_TYPES:
            raise ValueError(f"不支持的资料源类型：{connector_type}")
        if sync_mode not in SYNC_MODES:
            raise ValueError(f"不支持的同步模式：{sync_mode}")
        if domain not in DOMAINS:
            raise ValueError(f"不支持的知识领域：{domain}")
        if privacy not in PRIVACY_LEVELS:
            raise ValueError(f"不支持的隐私级别：{privacy}")
        root = self._validate_root(root_path)
        now = utc_now()
        root_uri = str(root)
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id, created_at FROM sync_roots WHERE connector_type = ? AND root_uri = ?",
                (connector_type, root_uri),
            ).fetchone()
            root_id = existing["id"] if existing else new_id("syn")
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO sync_roots(
                    id, name, root_uri, connector_type, domain, privacy, sync_mode,
                    recursive, enabled, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(connector_type, root_uri) DO UPDATE SET
                    name = excluded.name,
                    domain = excluded.domain,
                    privacy = excluded.privacy,
                    sync_mode = excluded.sync_mode,
                    recursive = excluded.recursive,
                    enabled = 1,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (
                    root_id,
                    name.strip() or root.name,
                    root_uri,
                    connector_type,
                    domain,
                    privacy,
                    sync_mode,
                    int(recursive),
                    json.dumps(config or {}, ensure_ascii=False),
                    created_at,
                    now,
                ),
            )
            Repository._audit(
                connection,
                "sync_root_registered",
                "sync_root",
                root_id,
                {"connector_type": connector_type, "sync_mode": sync_mode},
            )
            connection.commit()
        return self.get_root(root_id) or {}

    def get_root(self, root_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM sync_roots WHERE id = ?", (root_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["recursive"] = bool(item["recursive"])
        item["enabled"] = bool(item["enabled"])
        item["config"] = json.loads(item.pop("config_json"))
        return item

    def list_roots(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*,
                       COALESCE(s.item_count, 0) AS item_count,
                       COALESCE(s.active_count, 0) AS active_count,
                       COALESCE(s.indexed_count, 0) AS indexed_count,
                       COALESCE(s.missing_count, 0) AS missing_count,
                       COALESCE(s.skipped_count, 0) AS skipped_count,
                       COALESCE(s.error_count, 0) AS error_count,
                       COALESCE(s.last_result_json, '{}') AS last_result_json
                FROM sync_roots r
                LEFT JOIN sync_root_stats s ON s.root_id = r.id
                ORDER BY r.created_at ASC
                """
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["recursive"] = bool(item["recursive"])
            item["enabled"] = bool(item["enabled"])
            item["config"] = json.loads(item.pop("config_json"))
            item["item_count"] = int(item["item_count"] or 0)
            item["active_count"] = int(item["active_count"] or 0)
            item["indexed_count"] = int(item["indexed_count"] or 0)
            item["missing_count"] = int(item["missing_count"] or 0)
            item["skipped_count"] = int(item["skipped_count"] or 0)
            item["error_count"] = int(item["error_count"] or 0)
            item["last_result"] = json.loads(item.pop("last_result_json"))
            items.append(item)
        return items

    def search_catalog(
        self,
        query: str,
        *,
        root_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["i.state <> 'missing'", "i.relative_path LIKE ?"]
        params: list[Any] = [f"%{query.strip()}%"]
        if root_id:
            clauses.append("i.root_id = ?")
            params.append(root_id)
        params.append(max(1, min(limit, 200)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT i.id, i.root_id, r.name AS root_name, r.connector_type,
                       i.source_uri, i.relative_path, i.byte_size, i.modified_ns,
                       i.state, i.reason, i.source_id, i.last_seen_at
                FROM sync_items i
                JOIN sync_roots r ON r.id = i.root_id
                WHERE {' AND '.join(clauses)}
                ORDER BY i.modified_ns DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def scan_root(self, root_id: str) -> dict[str, Any]:
        root = self.get_root(root_id)
        if not root:
            raise ValueError("资料源不存在。")
        if not root["enabled"]:
            raise ValueError("资料源已停用。")
        root_path = self._validate_root(root["root_uri"])
        if root["connector_type"] == "codex_sessions":
            result = self._scan_codex_sessions(root, root_path)
        else:
            result = self._scan_local_files(root, root_path)
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE sync_roots SET last_scan_at = ?, updated_at = ? WHERE id = ?",
                (now, now, root_id),
            )
            Repository._audit(connection, "sync_root_scanned", "sync_root", root_id, result)
            connection.commit()
        self.refresh_root_stats(root_id, result=result, updated_at=now)
        return result

    def refresh_root_stats(
        self,
        root_id: str,
        *,
        result: dict[str, Any] | None = None,
        updated_at: str | None = None,
    ) -> None:
        now = updated_at or utc_now()
        with self.database.connect() as connection:
            counts = connection.execute(
                """
                SELECT COUNT(*) AS item_count,
                       SUM(CASE WHEN state <> 'missing' THEN 1 ELSE 0 END) AS active_count,
                       SUM(CASE WHEN state = 'indexed' THEN 1 ELSE 0 END) AS indexed_count,
                       SUM(CASE WHEN state = 'missing' THEN 1 ELSE 0 END) AS missing_count,
                       SUM(CASE WHEN state = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                       SUM(CASE WHEN state = 'error' THEN 1 ELSE 0 END) AS error_count
                FROM sync_items WHERE root_id = ?
                """,
                (root_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO sync_root_stats(
                    root_id, item_count, active_count, indexed_count, missing_count,
                    skipped_count, error_count, last_result_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_id) DO UPDATE SET
                    item_count = excluded.item_count,
                    active_count = excluded.active_count,
                    indexed_count = excluded.indexed_count,
                    missing_count = excluded.missing_count,
                    skipped_count = excluded.skipped_count,
                    error_count = excluded.error_count,
                    last_result_json = excluded.last_result_json,
                    updated_at = excluded.updated_at
                """,
                (
                    root_id,
                    int(counts["item_count"] or 0),
                    int(counts["active_count"] or 0),
                    int(counts["indexed_count"] or 0),
                    int(counts["missing_count"] or 0),
                    int(counts["skipped_count"] or 0),
                    int(counts["error_count"] or 0),
                    json.dumps(result or {}, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()

    @staticmethod
    def _external_id(relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/").casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _walk_files(self, root: Path, recursive: bool) -> Any:
        if not recursive:
            for path in root.iterdir():
                if path.is_file() and not path.is_symlink():
                    yield path
            return
        for directory, names, filenames in os.walk(root, followlinks=False):
            names[:] = [name for name in names if name.casefold() not in SKIP_DIRECTORIES]
            base = Path(directory)
            for filename in filenames:
                path = base / filename
                if not path.is_symlink():
                    yield path

    def _existing_item(self, root_id: str, external_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_items WHERE root_id = ? AND external_id = ?",
                (root_id, external_id),
            ).fetchone()
        return dict(row) if row else None

    def _existing_items(self, root_id: str) -> dict[str, dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sync_items WHERE root_id = ?",
                (root_id,),
            ).fetchall()
        return {row["external_id"]: dict(row) for row in rows}

    def _upsert_item(
        self,
        *,
        root_id: str,
        external_id: str,
        source_uri: str,
        relative_path: str,
        byte_size: int,
        modified_ns: int,
        fingerprint: str,
        state: str,
        reason: str | None,
        source_id: str | None,
        metadata: dict[str, Any] | None,
        scan_time: str,
        indexed_at: str | None,
    ) -> None:
        self._upsert_items(
            [
                {
                    "root_id": root_id,
                    "external_id": external_id,
                    "source_uri": source_uri,
                    "relative_path": relative_path,
                    "byte_size": byte_size,
                    "modified_ns": modified_ns,
                    "fingerprint": fingerprint,
                    "state": state,
                    "reason": reason,
                    "source_id": source_id,
                    "metadata": metadata,
                    "scan_time": scan_time,
                    "indexed_at": indexed_at,
                }
            ]
        )

    def _upsert_items(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        statement = """
            INSERT INTO sync_items(
                id, root_id, external_id, source_uri, relative_path, byte_size,
                modified_ns, fingerprint, state, reason, source_id, metadata_json,
                first_seen_at, last_seen_at, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(root_id, external_id) DO UPDATE SET
                source_uri = excluded.source_uri,
                relative_path = excluded.relative_path,
                byte_size = excluded.byte_size,
                modified_ns = excluded.modified_ns,
                fingerprint = excluded.fingerprint,
                state = excluded.state,
                reason = excluded.reason,
                source_id = excluded.source_id,
                metadata_json = excluded.metadata_json,
                last_seen_at = excluded.last_seen_at,
                indexed_at = COALESCE(excluded.indexed_at, sync_items.indexed_at)
        """
        rows = [
            (
                new_id("sni"),
                item["root_id"],
                item["external_id"],
                item["source_uri"],
                item["relative_path"],
                item["byte_size"],
                item["modified_ns"],
                item["fingerprint"],
                item["state"],
                item["reason"],
                item["source_id"],
                json.dumps(item.get("metadata") or {}, ensure_ascii=False),
                item["scan_time"],
                item["scan_time"],
                item["indexed_at"],
            )
            for item in items
        ]
        with self.database.connect() as connection:
            connection.executemany(statement, rows)
            connection.commit()

    def _mark_missing(self, root_id: str, scan_time: str) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sync_items
                SET state = 'missing', reason = 'not_seen_in_latest_scan'
                WHERE root_id = ? AND last_seen_at <> ? AND state <> 'missing'
                """,
                (root_id, scan_time),
            )
            connection.commit()
        return max(0, cursor.rowcount)

    def _scan_local_files(self, root: dict[str, Any], root_path: Path) -> dict[str, Any]:
        scan_time = utc_now()
        counts = {
            "files_seen": 0,
            "cataloged": 0,
            "indexed": 0,
            "duplicates": 0,
            "unchanged": 0,
            "skipped": 0,
            "unreadable": 0,
            "errors": 0,
            "bytes_seen": 0,
        }
        existing_items = self._existing_items(root["id"])
        pending_items: list[dict[str, Any]] = []
        for path in self._walk_files(root_path, root["recursive"]):
            counts["files_seen"] += 1
            relative = str(path.relative_to(root_path))
            try:
                stat = path.stat()
            except OSError as exc:
                counts["unreadable"] += 1
                external_id = self._external_id(relative)
                error_code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
                pending_items.append(
                    {
                        "root_id": root["id"],
                        "external_id": external_id,
                        "source_uri": str(path),
                        "relative_path": relative,
                        "byte_size": 0,
                        "modified_ns": 0,
                        "fingerprint": f"unreadable:{error_code or 'unknown'}",
                        "state": "error",
                        "reason": f"os_error_{error_code or 'unknown'}",
                        "source_id": None,
                        "metadata": {"extension": path.suffix.lower()},
                        "scan_time": scan_time,
                        "indexed_at": None,
                    }
                )
                if len(pending_items) >= 1000:
                    self._upsert_items(pending_items)
                    pending_items.clear()
                continue
            counts["bytes_seen"] += stat.st_size
            external_id = self._external_id(relative)
            fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
            existing = existing_items.get(external_id)
            state = "cataloged"
            reason: str | None = None
            source_id = existing.get("source_id") if existing else None
            indexed_at: str | None = None

            if is_sensitive_path(path):
                state = "skipped"
                reason = "sensitive_name"
                counts["skipped"] += 1
            elif root["sync_mode"] == "index" and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                if (
                    existing
                    and existing["fingerprint"] == fingerprint
                    and existing["state"] == "indexed"
                ):
                    state = "indexed"
                    counts["unchanged"] += 1
                else:
                    try:
                        imported = self.ingestion.import_file(
                            path,
                            domain=root["domain"],
                            privacy=root["privacy"],
                        )
                        new_source_id = imported["source_id"]
                        if source_id and source_id != new_source_id:
                            self.repository.set_source_status(source_id, "superseded")
                        self.repository.set_source_status(new_source_id, "indexed")
                        source_id = new_source_id
                        state = "indexed"
                        indexed_at = scan_time
                        if imported["status"] in {"imported", "reindexed"}:
                            counts["indexed"] += 1
                        else:
                            counts["duplicates"] += 1
                    except ParseError:
                        state = "skipped"
                        reason = "no_indexable_text"
                        counts["skipped"] += 1
                    except (OSError, ValueError) as exc:
                        state = "error"
                        reason = type(exc).__name__
                        counts["errors"] += 1
            elif root["sync_mode"] == "index":
                state = "skipped"
                reason = "unsupported_format"
                counts["skipped"] += 1
            else:
                counts["cataloged"] += 1

            pending_items.append(
                {
                    "root_id": root["id"],
                    "external_id": external_id,
                    "source_uri": str(path),
                    "relative_path": relative,
                    "byte_size": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                    "fingerprint": fingerprint,
                    "state": state,
                    "reason": reason,
                    "source_id": source_id,
                    "metadata": {"extension": path.suffix.lower()},
                    "scan_time": scan_time,
                    "indexed_at": indexed_at,
                }
            )
            if len(pending_items) >= 1000:
                self._upsert_items(pending_items)
                pending_items.clear()
        self._upsert_items(pending_items)
        counts["missing"] = self._mark_missing(root["id"], scan_time)
        return counts

    def _cursor(self, root_id: str, source_uri: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM codex_session_cursors
                WHERE root_id = ? AND source_uri = ?
                """,
                (root_id, source_uri),
            ).fetchone()
        return dict(row) if row else None

    def _save_cursor(
        self,
        *,
        root_id: str,
        source_uri: str,
        byte_offset: int,
        byte_size: int,
        modified_ns: int,
        metadata: dict[str, Any],
        scan_time: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO codex_session_cursors(
                    root_id, source_uri, byte_offset, byte_size, modified_ns,
                    metadata_json, last_scan_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_id, source_uri) DO UPDATE SET
                    byte_offset = excluded.byte_offset,
                    byte_size = excluded.byte_size,
                    modified_ns = excluded.modified_ns,
                    metadata_json = excluded.metadata_json,
                    last_scan_at = excluded.last_scan_at
                """,
                (
                    root_id,
                    source_uri,
                    byte_offset,
                    byte_size,
                    modified_ns,
                    json.dumps(metadata, ensure_ascii=False),
                    scan_time,
                ),
            )
            connection.commit()

    @staticmethod
    def _payload_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(SyncService._payload_text(item) for item in value).strip()
        if isinstance(value, dict):
            for key in ("text", "message", "content"):
                if key in value:
                    return SyncService._payload_text(value[key])
        return ""

    def _scan_codex_file(
        self,
        *,
        path: Path,
        offset: int,
        metadata: dict[str, Any],
        root: dict[str, Any],
    ) -> tuple[int, dict[str, Any], int, int]:
        session_id = str(metadata.get("session_id") or path.stem)
        thread_name = metadata.get("thread_name")
        cwd = metadata.get("cwd")
        current_turn_id = metadata.get("current_turn_id")
        current_started_at = metadata.get("current_started_at")
        pending_user = str(metadata.get("pending_user") or "")
        imported_count = 0
        duplicate_count = 0

        with path.open("rb") as handle:
            handle.seek(offset)
            for raw_line in handle:
                if not any(
                    marker in raw_line
                    for marker in (
                        b'"session_meta"',
                        b'"turn_context"',
                        b'"task_started"',
                        b'"user_message"',
                        b'"task_complete"',
                        b'"thread_name_updated"',
                    )
                ):
                    continue
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                outer_type = event.get("type")
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if outer_type == "session_meta":
                    session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                    cwd = payload.get("cwd") or cwd
                    continue
                if outer_type == "turn_context":
                    cwd = payload.get("cwd") or cwd
                    continue
                if outer_type != "event_msg":
                    continue
                event_type = payload.get("type")
                if event_type == "thread_name_updated":
                    thread_name = payload.get("thread_name") or thread_name
                elif event_type == "task_started":
                    current_turn_id = payload.get("turn_id") or current_turn_id
                    current_started_at = payload.get("started_at") or event.get("timestamp")
                    pending_user = ""
                elif event_type == "user_message":
                    message = self._payload_text(payload.get("message"))
                    if message:
                        pending_user = f"{pending_user}\n{message}".strip()
                elif event_type == "task_complete":
                    turn_id = str(payload.get("turn_id") or current_turn_id or "")
                    if not turn_id:
                        seed = f"{path}:{payload.get('completed_at')}:{handle.tell()}"
                        turn_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
                    if pending_user and not is_internal_codex_turn(
                        user_text=pending_user,
                        cwd=str(cwd) if cwd else None,
                    ):
                        turn = build_codex_turn(
                            thread_id=session_id,
                            turn_id=turn_id,
                            user_text=pending_user,
                            assistant_text="",
                            cwd=str(cwd) if cwd else None,
                            thread_name=str(thread_name) if thread_name else None,
                            started_at=(
                                str(payload.get("started_at") or current_started_at or "") or None
                            ),
                            completed_at=(
                                str(payload.get("completed_at") or event.get("timestamp") or "")
                            )
                            or None,
                            capture_mode="historical-session",
                        )
                        result = self.ingestion.import_text(
                            text=turn["text"],
                            title=turn["title"],
                            original_uri=turn["original_uri"],
                            source_type="codex-turn",
                            domain=root["domain"],
                            privacy=root["privacy"],
                            metadata=turn["metadata"],
                            event_time=turn["event_time"],
                            source_created_at=turn["event_time"],
                        )
                        if result["status"] == "imported":
                            imported_count += 1
                        else:
                            duplicate_count += 1
                    pending_user = ""
                    current_turn_id = None
                    current_started_at = None
            final_offset = handle.tell()

        safe_pending, _ = redact_secrets(pending_user[:200_000])
        next_metadata = {
            "session_id": session_id,
            "thread_name": thread_name,
            "cwd": cwd,
            "current_turn_id": current_turn_id,
            "current_started_at": current_started_at,
            "pending_user": safe_pending,
        }
        return final_offset, next_metadata, imported_count, duplicate_count

    def _scan_codex_sessions(self, root: dict[str, Any], root_path: Path) -> dict[str, Any]:
        scan_time = utc_now()
        counts = {
            "files_seen": 0,
            "bytes_read": 0,
            "turns_indexed": 0,
            "turns_duplicate": 0,
            "unchanged": 0,
            "errors": 0,
        }
        for path in self._walk_files(root_path, root["recursive"]):
            if path.suffix.lower() != ".jsonl":
                continue
            counts["files_seen"] += 1
            try:
                stat = path.stat()
                relative = str(path.relative_to(root_path))
                source_uri = str(path)
                cursor = self._cursor(root["id"], source_uri)
                offset = int(cursor["byte_offset"]) if cursor else 0
                metadata = json.loads(cursor["metadata_json"]) if cursor else {}
                if stat.st_size < offset:
                    offset = 0
                    metadata = {}
                if stat.st_size == offset:
                    counts["unchanged"] += 1
                    next_offset = offset
                    next_metadata = metadata
                    imported = 0
                    duplicates = 0
                else:
                    next_offset, next_metadata, imported, duplicates = self._scan_codex_file(
                        path=path,
                        offset=offset,
                        metadata=metadata,
                        root=root,
                    )
                    counts["bytes_read"] += max(0, next_offset - offset)
                    counts["turns_indexed"] += imported
                    counts["turns_duplicate"] += duplicates
                self._save_cursor(
                    root_id=root["id"],
                    source_uri=source_uri,
                    byte_offset=next_offset,
                    byte_size=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    metadata=next_metadata,
                    scan_time=scan_time,
                )
                self._upsert_item(
                    root_id=root["id"],
                    external_id=self._external_id(relative),
                    source_uri=source_uri,
                    relative_path=relative,
                    byte_size=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    fingerprint=f"{stat.st_size}:{stat.st_mtime_ns}",
                    state="indexed",
                    reason=None,
                    source_id=None,
                    metadata={
                        "cursor_offset": next_offset,
                        "turns_indexed_in_scan": imported,
                    },
                    scan_time=scan_time,
                    indexed_at=scan_time if imported else None,
                )
            except (OSError, ValueError, TypeError):
                counts["errors"] += 1
        counts["missing"] = self._mark_missing(root["id"], scan_time)
        return counts
