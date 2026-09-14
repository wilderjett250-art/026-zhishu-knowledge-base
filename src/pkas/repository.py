import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.db import Database
from pkas.parsers import ParsedBlock, ParsedDocument

CODEX_TURN_EVIDENCE_STATUS = "user_request_only"
CODEX_TURN_EVIDENCE_WARNING = (
    "该资料只记录用户当时提出的任务，不包含 Codex 的回答，也不是完成证据；"
    "修改、测试、部署或验收状态必须通过当前文件、Git、测试输出或外部回执核验。"
)
CODEX_USER_TASK_MIGRATION_KEY = "codex_turn_user_task_only_v1"


def _extract_codex_user_task_text(text: str) -> str | None:
    """Return only the structured user-request section from a legacy Codex turn."""
    marker = "## 用户请求"
    if marker not in text:
        return None
    prefix, remainder = text.split(marker, 1)
    user_text = re.split(
        r"(?m)^## Codex 最终回答[^\r\n]*(?:\r?\n)?",
        remainder,
        maxsplit=1,
    )[0].strip()
    if not user_text or user_text == "（本轮没有可索引的文字请求）":
        return None
    return f"{prefix.rstrip()}\n\n{marker}\n\n{user_text}".strip()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Repository:
    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()
        self.database.initialize()
        if self.get_app_meta("document_title_repair_v1") != "completed":
            self.repair_hashed_document_titles()
            self.set_app_meta("document_title_repair_v1", "completed")
        if self.get_app_meta(CODEX_USER_TASK_MIGRATION_KEY) is None:
            with self.database.connect() as connection:
                codex_turn_count = connection.execute(
                    "SELECT count(*) AS count FROM sources WHERE source_type = 'codex-turn'"
                ).fetchone()["count"]
            if codex_turn_count == 0:
                empty_result = {
                    "status": "completed",
                    "records_seen": 0,
                    "user_tasks_migrated": 0,
                    "legacy_records_excluded": 0,
                    "knowledge_candidates_invalidated": 0,
                }
                self.set_app_meta(
                    f"{CODEX_USER_TASK_MIGRATION_KEY}_result",
                    json.dumps(empty_result, ensure_ascii=False),
                )
                self.set_app_meta(CODEX_USER_TASK_MIGRATION_KEY, "completed")

    def get_app_meta(self, key: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_app_meta(self, key: str, value: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, ?)",
                (key, value),
            )
            connection.commit()

    def repair_hashed_document_titles(self) -> int:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.id AS document_id, s.original_name
                FROM documents d
                JOIN sources s ON s.id = d.source_id
                WHERE length(d.title) = 64
                  AND lower(d.title) NOT GLOB '*[^0-9a-f]*'
                """
            ).fetchall()
            repaired = 0
            for row in rows:
                title = Path(row["original_name"]).stem.strip()
                if not title:
                    continue
                connection.execute(
                    "UPDATE documents SET title = ? WHERE id = ?",
                    (title, row["document_id"]),
                )
                connection.execute(
                    "UPDATE chunks SET title = ? WHERE document_id = ?",
                    (title, row["document_id"]),
                )
                connection.execute(
                    """
                    UPDATE chunks_fts SET title = ?
                    WHERE chunk_id IN (
                        SELECT id FROM chunks WHERE document_id = ?
                    )
                    """,
                    (title, row["document_id"]),
                )
                repaired += 1
            connection.commit()
        return repaired

    def migrate_codex_turns_to_user_tasks(self) -> dict[str, int | str]:
        """Remove assistant outputs from legacy derived Codex records and their FTS chunks."""
        from pkas.ingest import chunk_text

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id AS source_id, s.vault_path, s.status, s.metadata_json,
                       s.domain, s.privacy, d.id AS document_id, d.title,
                       d.text_content, d.language, d.event_time
                FROM sources s
                JOIN documents d ON d.source_id = s.id
                WHERE s.source_type = 'codex-turn'
                  AND (
                      COALESCE(json_extract(s.metadata_json, '$.record_kind'), '') <> 'user_task'
                      OR COALESCE(
                          json_extract(s.metadata_json, '$.assistant_output_indexed'), 1
                      ) <> 0
                      OR instr(d.text_content, '## Codex 最终回答') > 0
                  )
                ORDER BY s.ingested_at ASC
                """
            ).fetchall()

        batch_size = 500
        with self.database.connect() as connection:
            connection.execute(
                "CREATE TEMP TABLE codex_migration_sources(source_id TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO codex_migration_sources(source_id) VALUES(?)",
                [(row["source_id"],) for row in rows],
            )
            connection.execute(
                """
                DELETE FROM messages
                WHERE document_id IN (
                    SELECT d.id
                    FROM documents d
                    JOIN codex_migration_sources m ON m.source_id = d.source_id
                )
                """
            )
            connection.execute(
                """
                DELETE FROM chunks
                WHERE source_id IN (SELECT source_id FROM codex_migration_sources)
                """
            )
            connection.commit()
            for index, row in enumerate(rows, start=1):
                metadata = json.loads(row["metadata_json"] or "{}")
                user_task_text = _extract_codex_user_task_text(row["text_content"])
                if user_task_text:
                    new_text = user_task_text
                    source_status = str(row["status"])
                    chunks = chunk_text(new_text) if source_status == "indexed" else []
                    metadata["record_kind"] = "user_task"
                    metadata["evidence_basis"] = "user_request_only"
                else:
                    new_text = (
                        "# 已排除的旧版 Codex 记录\n\n"
                        f"- source_id：{row['source_id']}\n\n"
                        "旧版记录无法可靠区分用户任务与系统输出，已从检索层排除；"
                        "原始 Codex 会话文件未修改。"
                    )
                    source_status = "superseded"
                    chunks = []
                    metadata["record_kind"] = "excluded_legacy_codex_record"
                    metadata["evidence_basis"] = "excluded_unclassified_legacy_record"
                metadata["assistant_output_indexed"] = False
                metadata["migration_version"] = "user-task-only-v1"
                for key in (
                    "assistant_claim_status",
                    "assistant_truncated",
                    "completion_verified",
                ):
                    metadata.pop(key, None)

                raw = new_text.encode("utf-8")
                content_hash = hashlib.sha256(raw).hexdigest()
                collision = connection.execute(
                    "SELECT id FROM sources WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if collision and collision["id"] != row["source_id"]:
                    new_text = f"{new_text}\n\n- 记录标识：{row['source_id']}"
                    raw = new_text.encode("utf-8")
                    content_hash = hashlib.sha256(raw).hexdigest()
                    chunks = chunk_text(new_text) if source_status == "indexed" else []

                vault_path = Path(row["vault_path"])
                vault_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = vault_path.with_name(
                    f"{vault_path.name}.user-task-only.tmp"
                )
                temporary_path.write_bytes(raw)
                if hashlib.sha256(temporary_path.read_bytes()).hexdigest() != content_hash:
                    temporary_path.unlink(missing_ok=True)
                    raise OSError("Codex 用户任务派生文件写入后的哈希校验失败。")
                temporary_path.replace(vault_path)

                connection.execute(
                    """
                    UPDATE sources
                    SET mime_type = 'text/markdown', metadata_json = ?, status = ?,
                        content_hash = ?, byte_size = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(metadata, ensure_ascii=False),
                        source_status,
                        content_hash,
                        len(raw),
                        row["source_id"],
                    ),
                )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE documents
                    SET text_content = ?, language = ?, event_time = ?,
                        parser_name = 'normalized-text', parser_version = '2',
                        created_at = ?
                    WHERE id = ?
                    """,
                    (new_text, row["language"], row["event_time"], now, row["document_id"]),
                )
                for chunk in chunks:
                    chunk_id = new_id("chk")
                    connection.execute(
                        """
                        INSERT INTO chunks(
                            id, document_id, source_id, sequence, title, text_content,
                            locator, domain, privacy, char_count, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            row["document_id"],
                            row["source_id"],
                            chunk["sequence"],
                            row["title"],
                            chunk["text"],
                            chunk["locator"],
                            row["domain"],
                            row["privacy"],
                            len(chunk["text"]),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO chunks_fts(chunk_id, title, content, domain, privacy)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            row["title"],
                            chunk["text"],
                            row["domain"],
                            row["privacy"],
                        ),
                    )
                if index % batch_size == 0:
                    self._audit(
                        connection,
                        "codex_user_task_migration_batch",
                        "source",
                        None,
                        {"processed": index, "batch_size": batch_size},
                    )
                    connection.commit()
            connection.commit()
            connection.execute("DROP TABLE chunks_fts")
            tokenizer = self.database._ensure_fts(connection)
            connection.execute(
                """
                INSERT INTO chunks_fts(chunk_id, title, content, domain, privacy)
                SELECT id, title, text_content, domain, privacy FROM chunks
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES('fts_tokenizer', ?)",
                (tokenizer,),
            )
            connection.commit()

        now = utc_now()
        with self.database.connect() as connection:
            legacy_items = connection.execute(
                """
                SELECT k.id
                FROM knowledge_items k
                WHERE EXISTS (
                    SELECT 1
                    FROM evidence_links e
                    LEFT JOIN sources s ON s.id = e.evidence_id
                    LEFT JOIN documents d ON d.id = e.evidence_id
                    LEFT JOIN sources ds ON ds.id = d.source_id
                    WHERE e.subject_id = k.id
                      AND COALESCE(s.source_type, ds.source_type, '') = 'codex-turn'
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM evidence_links e
                    LEFT JOIN sources s ON s.id = e.evidence_id
                    LEFT JOIN documents d ON d.id = e.evidence_id
                    LEFT JOIN sources ds ON ds.id = d.source_id
                    WHERE e.subject_id = k.id
                      AND COALESCE(s.source_type, ds.source_type, '') <> 'codex-turn'
                )
                """
            ).fetchall()
            if legacy_items:
                item_ids = [row["id"] for row in legacy_items]
                placeholders = ",".join("?" for _ in item_ids)
                connection.execute(
                    f"""
                    UPDATE knowledge_items
                    SET title = '已排除的旧版 Codex 收尾候选',
                        content = '旧版候选只由 Codex 线程记录派生，缺少独立机器证据，已停止使用。',
                        confidence = 'low', review_status = 'rejected',
                        valid_to = COALESCE(valid_to, ?), updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now, now, *item_ids],
                )
                self._audit(
                    connection,
                    "legacy_codex_candidates_invalidated",
                    "knowledge_item",
                    None,
                    {"count": len(item_ids)},
                )
                connection.commit()
            aggregate = connection.execute(
                """
                SELECT count(*) AS total,
                       sum(
                           CASE WHEN json_extract(metadata_json, '$.record_kind') = 'user_task'
                                THEN 1 ELSE 0 END
                       ) AS user_tasks,
                       sum(
                           CASE WHEN json_extract(metadata_json, '$.record_kind') =
                                     'excluded_legacy_codex_record'
                                THEN 1 ELSE 0 END
                       ) AS excluded
                FROM sources
                WHERE source_type = 'codex-turn'
                """
            ).fetchone()
            self._audit(
                connection,
                "codex_user_task_migration_completed",
                "source",
                None,
                {
                    "records_seen": int(aggregate["total"] or 0),
                    "records_processed_this_run": len(rows),
                    "knowledge_candidates_invalidated": len(legacy_items),
                },
            )
            connection.commit()

        return {
            "status": "completed",
            "records_seen": int(aggregate["total"] or 0),
            "records_processed_this_run": len(rows),
            "user_tasks_migrated": int(aggregate["user_tasks"] or 0),
            "legacy_records_excluded": int(aggregate["excluded"] or 0),
            "knowledge_candidates_invalidated": len(legacy_items),
        }

    def run_codex_turn_user_task_migration(self) -> dict[str, Any]:
        """Claim and run the one-time migration without racing normal MCP startup."""
        result_key = f"{CODEX_USER_TASK_MIGRATION_KEY}_result"
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT value FROM app_meta WHERE key = ?",
                (CODEX_USER_TASK_MIGRATION_KEY,),
            ).fetchone()
            if current and current["value"] == "completed":
                stored = connection.execute(
                    "SELECT value FROM app_meta WHERE key = ?",
                    (result_key,),
                ).fetchone()
                connection.commit()
                return {
                    "status": "already_completed",
                    "result": json.loads(stored["value"]) if stored else None,
                }
            if current and current["value"] == "in_progress":
                connection.commit()
                return {"status": "already_running"}
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, 'in_progress')",
                (CODEX_USER_TASK_MIGRATION_KEY,),
            )
            connection.commit()

        try:
            result = self.migrate_codex_turns_to_user_tasks()
        except Exception:
            self.set_app_meta(CODEX_USER_TASK_MIGRATION_KEY, "failed")
            raise
        self.set_app_meta(result_key, json.dumps(result, ensure_ascii=False))
        self.set_app_meta(CODEX_USER_TASK_MIGRATION_KEY, "completed")
        return result

    def source_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, d.id AS document_id, d.parser_name, d.parser_version
                FROM sources s LEFT JOIN documents d ON d.source_id = s.id
                WHERE s.content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _annotate_evidence_status(item: dict[str, Any]) -> dict[str, Any]:
        if item.get("source_type") == "codex-turn":
            item["evidence_status"] = CODEX_TURN_EVIDENCE_STATUS
            item["evidence_warning"] = CODEX_TURN_EVIDENCE_WARNING
        return item

    def source_by_uri(self, original_uri: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM sources
                WHERE original_uri = ?
                ORDER BY ingested_at DESC
                LIMIT 1
                """,
                (original_uri,),
            ).fetchone()
        return dict(row) if row else None

    def set_source_status(self, source_id: str, status: str) -> None:
        if status not in {"indexed", "superseded"}:
            raise ValueError(f"不支持的资料状态：{status}")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE sources SET status = ? WHERE id = ?",
                (status, source_id),
            )
            connection.commit()

    @staticmethod
    def _document_blocks(parsed: ParsedDocument) -> list[ParsedBlock]:
        if parsed.blocks:
            return parsed.blocks
        return [
            ParsedBlock(
                kind="document-body",
                text=parsed.text,
                locator="document:body",
            )
        ]

    def _insert_blocks_and_chunks(
        self,
        connection: sqlite3.Connection,
        *,
        document_id: str,
        source_id: str,
        title: str,
        domain: str,
        privacy: str,
        parsed: ParsedDocument,
        chunks: list[dict[str, Any]],
        now: str,
    ) -> tuple[int, int]:
        blocks = self._document_blocks(parsed)
        block_ids: dict[int, str] = {}
        current_heading_id: str | None = None
        for sequence, block in enumerate(blocks):
            block_id = new_id("blk")
            parent_id = None if block.kind == "heading" else current_heading_id
            connection.execute(
                """
                INSERT INTO blocks(
                    id, document_id, sequence, parent_id, kind, text_content,
                    locator, page, bbox_json, metadata_json, char_count,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    block_id,
                    document_id,
                    sequence,
                    parent_id,
                    block.kind,
                    block.text,
                    block.locator,
                    block.page,
                    json.dumps(block.bbox) if block.bbox is not None else None,
                    json.dumps(block.metadata, ensure_ascii=False),
                    len(block.text),
                    hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
                    now,
                ),
            )
            block_ids[sequence] = block_id
            if block.kind == "heading":
                current_heading_id = block_id

        for chunk in chunks:
            chunk_id = new_id("chk")
            sequences = [
                int(sequence)
                for sequence in chunk.get("block_sequences", [])
                if int(sequence) in block_ids
            ]
            if not sequences and block_ids:
                sequences = [0]
            primary_block_id = block_ids.get(sequences[0]) if sequences else None
            connection.execute(
                """
                INSERT INTO chunks(
                    id, document_id, source_id, sequence, title, text_content,
                    locator, domain, privacy, char_count, block_id, chunk_kind,
                    chunker_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    document_id,
                    source_id,
                    chunk["sequence"],
                    title,
                    chunk["text"],
                    chunk["locator"],
                    domain,
                    privacy,
                    len(chunk["text"]),
                    primary_block_id,
                    chunk.get("chunk_kind", "legacy"),
                    chunk.get("chunker_version", "legacy-v1"),
                    now,
                ),
            )
            for relation_sequence, block_sequence in enumerate(sequences):
                connection.execute(
                    """
                    INSERT INTO chunk_blocks(chunk_id, block_id, sequence, is_primary)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        block_ids[block_sequence],
                        relation_sequence,
                        int(relation_sequence == 0),
                    ),
                )
            connection.execute(
                """
                INSERT INTO chunks_fts(chunk_id, title, content, domain, privacy)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chunk_id, title, chunk["text"], domain, privacy),
            )
        return len(blocks), len(chunks)

    def add_document(
        self,
        *,
        original_uri: str,
        original_name: str,
        vault_path: str,
        source_type: str,
        content_hash: str,
        byte_size: int,
        domain: str,
        privacy: str,
        parsed: ParsedDocument,
        chunks: list[dict[str, Any]],
        source_created_at: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        source_id = new_id("src")
        document_id = new_id("doc")
        with self.database.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO sources(
                        id, original_uri, original_name, vault_path, source_type,
                        content_hash, byte_size, mime_type, domain, privacy, status,
                        created_at, ingested_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'indexed', ?, ?, ?)
                    """,
                    (
                        source_id,
                        original_uri,
                        original_name,
                        vault_path,
                        source_type,
                        content_hash,
                        byte_size,
                        parsed.mime_type,
                        domain,
                        privacy,
                        source_created_at,
                        now,
                        json.dumps(parsed.metadata, ensure_ascii=False),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO documents(
                        id, source_id, title, text_content, language, event_time,
                        parser_name, parser_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        source_id,
                        parsed.title,
                        parsed.text,
                        parsed.language,
                        parsed.event_time,
                        parsed.parser_name,
                        parsed.parser_version,
                        now,
                    ),
                )
                for message in parsed.messages:
                    connection.execute(
                        """
                        INSERT INTO messages(
                            id, document_id, conversation_id, sequence, speaker,
                            sent_at, text_content, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("msg"),
                            document_id,
                            message.conversation_id,
                            message.sequence,
                            message.speaker,
                            message.sent_at,
                            message.text,
                            json.dumps(message.metadata, ensure_ascii=False),
                        ),
                    )
                block_count, _ = self._insert_blocks_and_chunks(
                    connection,
                    document_id=document_id,
                    source_id=source_id,
                    title=parsed.title,
                    domain=domain,
                    privacy=privacy,
                    parsed=parsed,
                    chunks=chunks,
                    now=now,
                )
                self._audit(
                    connection,
                    "source_ingested",
                    "source",
                    source_id,
                    {
                        "document_id": document_id,
                        "block_count": block_count,
                        "chunk_count": len(chunks),
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "source_id": source_id,
            "document_id": document_id,
            "chunk_count": len(chunks),
            "block_count": len(self._document_blocks(parsed)),
            "message_count": len(parsed.messages),
        }

    def replace_document(
        self,
        *,
        source_id: str,
        parsed: ParsedDocument,
        chunks: list[dict[str, Any]],
        source_status: str = "indexed",
        content_hash: str | None = None,
        byte_size: int | None = None,
        preserve_ingested_at: bool = False,
    ) -> dict[str, Any]:
        if source_status not in {"indexed", "superseded"}:
            raise ValueError(f"不支持的资料状态：{source_status}")
        now = utc_now()
        with self.database.connect() as connection:
            source = connection.execute(
                "SELECT domain, privacy, ingested_at FROM sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            document = connection.execute(
                "SELECT id FROM documents WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if not source or not document:
                raise ValueError("待重建索引的资料不存在。")
            document_id = document["id"]
            try:
                connection.execute(
                    """
                    DELETE FROM chunks_fts
                    WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id = ?)
                    """,
                    (source_id,),
                )
                connection.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
                connection.execute("DELETE FROM blocks WHERE document_id = ?", (document_id,))
                connection.execute("DELETE FROM messages WHERE document_id = ?", (document_id,))
                connection.execute(
                    """
                    UPDATE sources
                    SET mime_type = ?, metadata_json = ?, status = ?, ingested_at = ?,
                        content_hash = COALESCE(?, content_hash),
                        byte_size = COALESCE(?, byte_size)
                    WHERE id = ?
                    """,
                    (
                        parsed.mime_type,
                        json.dumps(parsed.metadata, ensure_ascii=False),
                        source_status,
                        source["ingested_at"] if preserve_ingested_at else now,
                        content_hash,
                        byte_size,
                        source_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE documents
                    SET title = ?, text_content = ?, language = ?, event_time = ?,
                        parser_name = ?, parser_version = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (
                        parsed.title,
                        parsed.text,
                        parsed.language,
                        parsed.event_time,
                        parsed.parser_name,
                        parsed.parser_version,
                        now,
                        document_id,
                    ),
                )
                for message in parsed.messages:
                    connection.execute(
                        """
                        INSERT INTO messages(
                            id, document_id, conversation_id, sequence, speaker,
                            sent_at, text_content, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("msg"),
                            document_id,
                            message.conversation_id,
                            message.sequence,
                            message.speaker,
                            message.sent_at,
                            message.text,
                            json.dumps(message.metadata, ensure_ascii=False),
                        ),
                    )
                block_count, _ = self._insert_blocks_and_chunks(
                    connection,
                    document_id=document_id,
                    source_id=source_id,
                    title=parsed.title,
                    domain=source["domain"],
                    privacy=source["privacy"],
                    parsed=parsed,
                    chunks=chunks,
                    now=now,
                )
                self._audit(
                    connection,
                    "source_reindexed",
                    "source",
                    source_id,
                    {
                        "document_id": document_id,
                        "parser_version": parsed.parser_version,
                        "block_count": block_count,
                        "chunk_count": len(chunks),
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "source_id": source_id,
            "document_id": document_id,
            "chunk_count": len(chunks),
            "block_count": len(self._document_blocks(parsed)),
            "message_count": len(parsed.messages),
        }

    def sources_for_reindex(
        self,
        parser_version: str,
        chunker_version: str,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.original_name, s.vault_path, s.source_type, s.privacy,
                       d.parser_name, d.parser_version
                FROM sources s JOIN documents d ON d.source_id = s.id
                WHERE s.status <> 'superseded'
                  AND d.parser_name <> 'normalized-text'
                  AND (
                    d.parser_version <> ?
                    OR EXISTS (
                        SELECT 1 FROM chunks c
                        WHERE c.document_id=d.id AND c.chunker_version <> ?
                    )
                  )
                ORDER BY s.ingested_at ASC
                LIMIT ?
                """,
                (parser_version, chunker_version, max(1, min(limit, 100_000))),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        event_type: str,
        subject_type: str | None,
        subject_id: str | None,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(event_type, subject_type, subject_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                subject_type,
                subject_id,
                json.dumps(details, ensure_ascii=False),
                utc_now(),
            ),
        )

    def stats(self) -> dict[str, Any]:
        with self.database.connect() as connection:
            counts = {}
            active_count_queries = {
                "sources": "SELECT COUNT(*) AS count FROM sources WHERE status = 'indexed'",
                "documents": (
                    "SELECT COUNT(*) AS count FROM documents d "
                    "JOIN sources s ON s.id = d.source_id WHERE s.status = 'indexed'"
                ),
                "chunks": (
                    "SELECT COUNT(*) AS count FROM chunks c "
                    "JOIN sources s ON s.id = c.source_id WHERE s.status = 'indexed'"
                ),
                "messages": (
                    "SELECT COUNT(*) AS count FROM messages m "
                    "JOIN documents d ON d.id = m.document_id "
                    "JOIN sources s ON s.id = d.source_id WHERE s.status = 'indexed'"
                ),
            }
            for table in (
                "sources",
                "documents",
                "chunks",
                "messages",
                "knowledge_items",
                "persona_observations",
                "distillation_examples",
                "workflow_runs",
                "agent_runs",
                "agent_steps",
                "agent_jobs",
                "llm_calls",
                "customers",
                "customer_conversations",
                "customer_messages",
                "customer_signals",
                "sync_roots",
                "sync_items",
            ):
                statement = active_count_queries.get(
                    table,
                    f"SELECT COUNT(*) AS count FROM {table}",
                )
                counts[table] = connection.execute(statement).fetchone()["count"]
            counts["knowledge_sources"] = connection.execute(
                """SELECT COUNT(*) AS count FROM sources
                WHERE status='indexed' AND source_type<>'codex-turn'"""
            ).fetchone()["count"]
            counts["knowledge_chunks"] = connection.execute(
                """SELECT COUNT(*) AS count FROM chunks c
                JOIN sources s ON s.id=c.source_id
                WHERE s.status='indexed' AND s.source_type<>'codex-turn'"""
            ).fetchone()["count"]
            counts["codex_user_tasks"] = connection.execute(
                """SELECT COUNT(*) AS count FROM sources
                WHERE status='indexed' AND source_type='codex-turn'
                  AND json_extract(metadata_json, '$.record_kind')='user_task'"""
            ).fetchone()["count"]
            domains = {
                row["domain"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT domain, COUNT(*) AS count
                    FROM sources
                    WHERE status='indexed' AND source_type<>'codex-turn'
                    GROUP BY domain
                    """
                ).fetchall()
            }
            recent_sources = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT s.id, s.original_name, s.source_type, s.domain, s.privacy,
                           s.ingested_at, d.id AS document_id,
                           COALESCE(NULLIF(d.title, ''), s.original_name) AS title
                    FROM sources s
                    LEFT JOIN documents d ON d.id = (
                        SELECT latest.id FROM documents latest
                        WHERE latest.source_id=s.id
                        ORDER BY latest.created_at DESC LIMIT 1
                    )
                    WHERE s.status='indexed' AND s.source_type<>'codex-turn'
                    ORDER BY s.ingested_at DESC LIMIT 8
                    """
                ).fetchall()
            ]
            recent_runs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, workflow_name, status, created_at, completed_at
                    FROM workflow_runs ORDER BY created_at DESC LIMIT 8
                    """
                ).fetchall()
            ]
        return {
            "counts": counts,
            "domains": domains,
            "recent_sources": recent_sources,
            "recent_runs": recent_runs,
        }

    def list_sources(
        self,
        limit: int = 100,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if status not in {None, "indexed", "superseded"}:
            raise ValueError(f"Unsupported source status: {status}")
        where_clause = " WHERE s.status = ?" if status else ""
        params: tuple[Any, ...] = (status, limit) if status else (limit,)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.id, s.original_uri, s.original_name, s.vault_path, s.source_type,
                       s.content_hash, s.byte_size, s.mime_type, s.domain, s.privacy,
                       s.status, s.created_at, s.ingested_at, d.id AS document_id,
                       COALESCE(NULLIF(d.title, ''), s.original_name) AS title
                FROM sources s
                LEFT JOIN documents d ON d.id = (
                    SELECT latest.id FROM documents latest
                    WHERE latest.source_id=s.id
                    ORDER BY latest.created_at DESC LIMIT 1
                ){where_clause} ORDER BY s.ingested_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_codex_turns_ingested_between(
        self,
        *,
        start_at: str,
        end_at: str,
    ) -> list[dict[str, Any]]:
        if self.get_app_meta(CODEX_USER_TASK_MIGRATION_KEY) != "completed":
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id AS source_id, s.original_uri, s.original_name,
                       s.privacy, s.ingested_at, s.metadata_json,
                       d.id AS document_id, d.title, d.text_content
                FROM sources s
                JOIN documents d ON d.source_id = s.id
                WHERE s.source_type = 'codex-turn'
                  AND s.status = 'indexed'
                  AND json_extract(s.metadata_json, '$.record_kind') = 'user_task'
                  AND json_extract(s.metadata_json, '$.assistant_output_indexed') = 0
                  AND COALESCE(
                        json_extract(s.metadata_json, '$.completed_at'),
                        s.ingested_at
                      ) > ?
                  AND COALESCE(
                        json_extract(s.metadata_json, '$.completed_at'),
                        s.ingested_at
                      ) <= ?
                ORDER BY COALESCE(
                    json_extract(s.metadata_json, '$.completed_at'),
                    s.ingested_at
                ) ASC
                """,
                (start_at, end_at),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["source_type"] = "codex-turn"
            self._annotate_evidence_status(item)
            items.append(item)
        return items

    @staticmethod
    def _fts_expression(query: str) -> str:
        tokens = [token.strip() for token in query.split() if token.strip()]
        if not tokens:
            return '""'
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def _search_exact_source_name(
        self,
        query: str,
        *,
        domain: str | None,
        include_restricted: bool,
        include_codex_turns: bool,
    ) -> list[dict[str, Any]]:
        clean = query.strip().strip('"').replace("/", "\\")
        source_name = clean.rsplit("\\", 1)[-1]
        if "." not in source_name:
            return []
        clauses = ["s.status = 'indexed'", "lower(s.original_name) = lower(?)"]
        params: list[Any] = [source_name]
        if domain:
            clauses.append("c.domain = ?")
            params.append(domain)
        if not include_restricted:
            clauses.append("c.privacy <> 'restricted'")
        if not include_codex_turns:
            clauses.append("s.source_type <> 'codex-turn'")
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, c.document_id, c.source_id, c.title,
                       c.locator, c.domain, c.privacy, s.original_uri, s.vault_path,
                       s.source_type,
                       substr(c.text_content, 1, 500) AS snippet, -100.0 AS score
                FROM sources s
                JOIN chunks c ON c.source_id = s.id
                WHERE {" AND ".join(clauses)}
                  AND c.sequence = 0
                ORDER BY s.ingested_at DESC
                LIMIT 10
                """,
                params,
            ).fetchall()
        results = [dict(row) for row in rows]
        for item in results:
            self._annotate_evidence_status(item)
            item["match_strategy"] = "exact_source_name"
        return results

    @staticmethod
    def _merge_results(limit: int, *groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        for group in groups:
            for item in group:
                source_key = str(item.get("source_id") or item.get("document_id") or "")
                if source_key and source_key in seen_sources:
                    continue
                if source_key:
                    seen_sources.add(source_key)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
        return merged

    @staticmethod
    def _broad_fts_expression(query: str) -> str:
        """Build a bounded OR query for natural-language recall.

        The primary query remains strict so short exact searches keep their precision. This
        fallback extracts ASCII terms and Chinese trigrams only when the strict query returns
        nothing. It deliberately stays deterministic and local; semantic model reranking is a
        separate, opt-in Agent step.
        """

        normalized = re.sub(r"\s+", " ", query.strip().lower())
        units = re.findall(r"[a-z0-9_./:\\-]+|[\u4e00-\u9fff]+", normalized)
        terms: list[str] = []
        for unit in units:
            if re.fullmatch(r"[\u4e00-\u9fff]+", unit):
                if len(unit) <= 4:
                    terms.append(unit)
                else:
                    terms.extend(unit[index : index + 3] for index in range(len(unit) - 2))
            elif len(unit) >= 2:
                terms.append(unit)

        unique_terms: list[str] = []
        for term in terms:
            if term not in unique_terms:
                unique_terms.append(term)
            if len(unique_terms) >= 24:
                break
        return " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in unique_terms
        )

    @staticmethod
    def _local_relevance(query: str, title: str, content: str, bm25_score: float) -> float:
        normalized_query = re.sub(r"\W+", "", query.lower(), flags=re.UNICODE)
        normalized_text = re.sub(
            r"\W+",
            "",
            f"{title}\n{content}".lower(),
            flags=re.UNICODE,
        )
        if not normalized_query:
            return -bm25_score
        query_grams = {
            normalized_query[index : index + 3]
            for index in range(max(1, len(normalized_query) - 2))
            if normalized_query[index : index + 3]
        }
        text_grams = {
            normalized_text[index : index + 3]
            for index in range(max(1, len(normalized_text) - 2))
            if normalized_text[index : index + 3]
        }
        overlap = len(query_grams & text_grams) / max(1, len(query_grams))
        exact_bonus = 2.0 if normalized_query in normalized_text else 0.0
        title_bonus = 0.75 if normalized_query in re.sub(r"\W+", "", title.lower()) else 0.0
        return exact_bonus + title_bonus + overlap + min(0.25, max(0.0, -bm25_score / 100.0))

    def _search_chunks_fts(
        self,
        *,
        expression: str,
        domain: str | None,
        limit: int,
        include_restricted: bool,
        include_codex_turns: bool,
        broad: bool,
        query: str,
    ) -> list[dict[str, Any]]:
        if not expression:
            return []
        clauses = ["s.status = 'indexed'"]
        params: list[Any] = [expression]
        if domain:
            clauses.append("c.domain = ?")
            params.append(domain)
        if not include_restricted:
            clauses.append("c.privacy <> 'restricted'")
        if not include_codex_turns:
            clauses.append("s.source_type <> 'codex-turn'")
        where_extra = "".join(f" AND {clause}" for clause in clauses)
        candidate_limit = min(100, max(limit, limit * 8)) if broad else limit
        params.append(candidate_limit)
        statement = f"""
            SELECT c.id AS chunk_id, c.document_id, c.source_id, c.title,
                   c.locator, c.domain, c.privacy, s.original_uri, s.vault_path,
                   s.source_type,
                   snippet(chunks_fts, 2, '', '', ' … ', 36) AS snippet,
                   bm25(chunks_fts, 0.0, 5.0, 1.0, 0.0, 0.0) AS score,
                   substr(c.text_content, 1, 4000) AS ranking_text
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            JOIN sources s ON s.id = c.source_id
            WHERE chunks_fts MATCH ? {where_extra}
            ORDER BY score
            LIMIT ?
        """
        try:
            with self.database.connect() as connection:
                rows = [dict(row) for row in connection.execute(statement, params).fetchall()]
        except sqlite3.OperationalError:
            return []
        if broad:
            for row in rows:
                row["local_relevance"] = self._local_relevance(
                    query,
                    row["title"],
                    row["ranking_text"],
                    float(row["score"]),
                )
            rows.sort(key=lambda item: (-item["local_relevance"], item["score"]))
        for row in rows:
            row.pop("ranking_text", None)
            self._annotate_evidence_status(row)
            row["match_strategy"] = "broad_local" if broad else "strict_fts"
        return rows[:limit]

    def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 10,
        include_restricted: bool = False,
        include_unverified_claims: bool = False,
        include_task_records: bool = True,
    ) -> list[dict[str, Any]]:
        # Kept for API compatibility. Codex assistant outputs are no longer indexed at all,
        # so there are no unverified assistant claims to opt into.
        _ = include_unverified_claims
        include_codex_turns = (
            include_task_records
            and self.get_app_meta(CODEX_USER_TASK_MIGRATION_KEY) == "completed"
        )
        query = query.strip()
        if not query:
            return []
        exact_sources = self._search_exact_source_name(
            query,
            domain=domain,
            include_restricted=include_restricted,
            include_codex_turns=include_codex_turns,
        )
        knowledge_results = self._search_knowledge_items(
            query,
            domain=domain,
            limit=limit,
            include_restricted=include_restricted,
        )
        rows = self._search_chunks_fts(
            expression=self._fts_expression(query),
            domain=domain,
            limit=limit,
            include_restricted=include_restricted,
            include_codex_turns=include_codex_turns,
            broad=False,
            query=query,
        )
        if rows:
            return self._merge_results(limit, exact_sources, rows, knowledge_results)

        rows = self._search_chunks_fts(
            expression=self._broad_fts_expression(query),
            domain=domain,
            limit=limit,
            include_restricted=include_restricted,
            include_codex_turns=include_codex_turns,
            broad=True,
            query=query,
        )
        if rows:
            return self._merge_results(limit, exact_sources, rows, knowledge_results)

        like_clauses = ["c.text_content LIKE ?", "s.status = 'indexed'"]
        like_params: list[Any] = [f"%{query}%"]
        if domain:
            like_clauses.append("c.domain = ?")
            like_params.append(domain)
        if not include_restricted:
            like_clauses.append("c.privacy <> 'restricted'")
        if not include_codex_turns:
            like_clauses.append("s.source_type <> 'codex-turn'")
        like_params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, c.document_id, c.source_id, c.title,
                       c.locator, c.domain, c.privacy, s.original_uri, s.vault_path,
                       s.source_type,
                       substr(c.text_content, 1, 500) AS snippet, 0.0 AS score
                FROM chunks c
                JOIN sources s ON s.id = c.source_id
                WHERE {" AND ".join(like_clauses)}
                ORDER BY c.created_at DESC
                LIMIT ?
                """,
                like_params,
            ).fetchall()
        results = [dict(row) for row in rows]
        for item in results:
            self._annotate_evidence_status(item)
            item["match_strategy"] = "literal_fallback"
        return self._merge_results(limit, exact_sources, results, knowledge_results)

    def _search_knowledge_items(
        self,
        query: str,
        *,
        domain: str | None,
        limit: int,
        include_restricted: bool,
    ) -> list[dict[str, Any]]:
        clauses = ["valid_to IS NULL", "review_status = 'approved'"]
        params: list[Any] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if not include_restricted:
            clauses.append("privacy <> 'restricted'")
        params.append(min(500, max(50, limit * 10)))
        with self.database.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT id, domain, knowledge_type, title, content, confidence,
                           review_status, privacy, valid_from, supersedes, created_at
                    FROM knowledge_items
                    WHERE {" AND ".join(clauses)}
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            ]
            evidence_by_item: dict[str, list[str]] = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                evidence_rows = connection.execute(
                    f"""
                    SELECT subject_id, evidence_id
                    FROM evidence_links
                    WHERE subject_id IN ({placeholders})
                    ORDER BY weight DESC, created_at ASC
                    """,
                    [row["id"] for row in rows],
                ).fetchall()
                for evidence in evidence_rows:
                    evidence_by_item.setdefault(evidence["subject_id"], []).append(
                        evidence["evidence_id"]
                    )

        ranked: list[dict[str, Any]] = []
        for row in rows:
            relevance = self._local_relevance(query, row["title"], row["content"], 0.0)
            if relevance <= 0:
                continue
            evidence_ids = evidence_by_item.get(row["id"], [])
            ranked.append(
                {
                    "chunk_id": None,
                    "document_id": row["id"],
                    "source_id": evidence_ids[0] if evidence_ids else None,
                    "title": row["title"],
                    "locator": f"knowledge-item:{row['id']}",
                    "domain": row["domain"],
                    "privacy": row["privacy"],
                    "original_uri": f"pkas://knowledge/{row['id']}",
                    "vault_path": None,
                    "snippet": row["content"][:500],
                    "score": -relevance,
                    "local_relevance": relevance,
                    "match_strategy": "knowledge_item",
                    "knowledge_type": row["knowledge_type"],
                    "review_status": row["review_status"],
                    "confidence": row["confidence"],
                    "evidence_ids": evidence_ids,
                }
            )
        ranked.sort(key=lambda item: -item["local_relevance"])
        return ranked[:limit]

    def read_document(
        self,
        document_id: str,
        offset: int = 0,
        limit: int = 12000,
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT d.id, d.title, d.text_content, d.parser_name, d.created_at,
                        s.id AS source_id, s.original_uri, s.vault_path, s.domain, s.privacy,
                        s.source_type, s.metadata_json
                FROM documents d JOIN sources s ON s.id = d.source_id
                WHERE d.id = ?
                """,
                (document_id,),
            ).fetchone()
            if not row and document_id.startswith("kni_"):
                knowledge = connection.execute(
                    """
                    SELECT id, title, content, domain, privacy, knowledge_type,
                           confidence, review_status, created_at
                    FROM knowledge_items WHERE id = ?
                    """,
                    (document_id,),
                ).fetchone()
                if knowledge:
                    data = dict(knowledge)
                    text = data.pop("content")
                    data.update(
                        {
                            "source_id": None,
                            "original_uri": f"pkas://knowledge/{document_id}",
                            "vault_path": None,
                            "parser_name": "knowledge-item",
                            "text": text[offset : offset + limit],
                            "offset": offset,
                            "returned_chars": len(text[offset : offset + limit]),
                            "total_chars": len(text),
                            "has_more": offset + limit < len(text),
                        }
                    )
                    return data
        if not row:
            return None
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json"))
        self._annotate_evidence_status(data)
        text = data.pop("text_content")
        data["text"] = text[offset : offset + limit]
        data["offset"] = offset
        data["returned_chars"] = len(data["text"])
        data["total_chars"] = len(text)
        data["has_more"] = offset + limit < len(text)
        return data

    def hydrate_chunks(
        self,
        chunk_ids: list[str],
        *,
        snippet_chars: int = 500,
        ranking_chars: int = 4000,
    ) -> dict[str, dict[str, Any]]:
        clean_ids = list(dict.fromkeys(chunk_id for chunk_id in chunk_ids if chunk_id))
        if not clean_ids:
            return {}
        placeholders = ",".join("?" for _ in clean_ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, c.document_id, c.source_id, c.sequence,
                       c.title, c.text_content, c.locator, c.domain, c.privacy,
                       c.block_id, c.chunk_kind, c.chunker_version, c.char_count,
                       s.original_uri, s.vault_path, s.source_type
                FROM chunks c JOIN sources s ON s.id=c.source_id
                WHERE c.id IN ({placeholders}) AND s.status='indexed'
                """,
                clean_ids,
            ).fetchall()
        items: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            text = str(item.pop("text_content"))
            item["snippet"] = text[:snippet_chars]
            item["ranking_text"] = text[:ranking_chars]
            self._annotate_evidence_status(item)
            items[item["chunk_id"]] = item
        return items

    def expand_chunk_context(
        self,
        chunk_id: str,
        *,
        radius: int = 1,
        max_chars: int = 6000,
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            anchor = connection.execute(
                "SELECT document_id, sequence, block_id FROM chunks WHERE id=?",
                (chunk_id,),
            ).fetchone()
            if not anchor:
                return None
            block_ids: list[str] = []
            if anchor["block_id"]:
                block = connection.execute(
                    "SELECT id, parent_id, sequence FROM blocks WHERE id=?",
                    (anchor["block_id"],),
                ).fetchone()
                if block and block["parent_id"]:
                    rows = connection.execute(
                        """
                        SELECT id AS entity_id, sequence, locator, text_content
                        FROM blocks WHERE id=? OR parent_id=? ORDER BY sequence
                        """,
                        (block["parent_id"], block["parent_id"]),
                    ).fetchall()
                elif block:
                    rows = connection.execute(
                        """
                        SELECT id AS entity_id, sequence, locator, text_content
                        FROM blocks
                        WHERE document_id=? AND sequence BETWEEN ? AND ?
                        ORDER BY sequence
                        """,
                        (
                            anchor["document_id"],
                            max(0, int(block["sequence"]) - radius),
                            int(block["sequence"]) + radius * 2,
                        ),
                    ).fetchall()
                else:
                    rows = []
                block_ids = [str(row["entity_id"]) for row in rows]
                related_chunk_ids: list[str] = []
                if block_ids:
                    placeholders = ",".join("?" for _ in block_ids)
                    related_chunk_ids = [
                        str(row["chunk_id"])
                        for row in connection.execute(
                            f"""SELECT DISTINCT chunk_id FROM chunk_blocks
                            WHERE block_id IN ({placeholders}) ORDER BY chunk_id""",
                            block_ids,
                        ).fetchall()
                    ]
            else:
                rows = connection.execute(
                    """
                    SELECT id AS entity_id, sequence, locator, text_content
                    FROM chunks
                    WHERE document_id=? AND sequence BETWEEN ? AND ?
                    ORDER BY sequence
                    """,
                    (
                        anchor["document_id"],
                        max(0, int(anchor["sequence"]) - radius),
                        int(anchor["sequence"]) + radius,
                    ),
                ).fetchall()
                related_chunk_ids = [str(row["entity_id"]) for row in rows]
        pieces: list[str] = []
        locators: list[str] = []
        included_blocks: list[str] = []
        for row in rows:
            marker = f"[{row['locator']}]\n"
            remaining = max_chars - sum(len(piece) for piece in pieces)
            if remaining <= len(marker):
                break
            content = str(row["text_content"])[: remaining - len(marker)]
            pieces.append(marker + content)
            locators.append(str(row["locator"]))
            included_blocks.append(str(row["entity_id"]))
        return {
            "document_id": str(anchor["document_id"]),
            "anchor_chunk_id": chunk_id,
            "chunk_ids": related_chunk_ids or [chunk_id],
            "block_ids": included_blocks if block_ids else [],
            "locators": locators,
            "text": "\n\n".join(pieces),
            "truncated": len(included_blocks) < len(rows),
        }

    def create_knowledge_candidate(
        self,
        *,
        domain: str,
        knowledge_type: str,
        title: str,
        content: str,
        confidence: str,
        privacy: str,
        evidence_ids: list[str],
    ) -> dict[str, Any]:
        if domain not in {"work", "self", "shared", "distill"}:
            raise ValueError(f"不支持的知识领域：{domain}")
        if confidence not in {"low", "medium", "high"}:
            raise ValueError(f"不支持的置信度：{confidence}")
        if privacy not in {"public", "private", "restricted"}:
            raise ValueError(f"不支持的隐私级别：{privacy}")
        clean_title = title.strip()[:300]
        clean_content = content.strip()
        if not clean_title or not clean_content:
            raise ValueError("知识标题和内容不能为空。")

        now = utc_now()
        item_id = new_id("kni")
        with self.database.connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM knowledge_items
                WHERE domain = ? AND knowledge_type = ? AND title = ? AND valid_to IS NULL
                ORDER BY updated_at DESC LIMIT 1
                """,
                (domain, knowledge_type, clean_title),
            ).fetchone()
            if existing and existing["content"] == clean_content:
                result = dict(existing)
                result["status"] = "duplicate"
                return result
            supersedes = existing["id"] if existing else None
            if existing:
                connection.execute(
                    "UPDATE knowledge_items SET valid_to = ?, updated_at = ? WHERE id = ?",
                    (now, now, existing["id"]),
                )
            connection.execute(
                """
                INSERT INTO knowledge_items(
                    id, domain, knowledge_type, title, content, confidence,
                    review_status, privacy, valid_from, supersedes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    domain,
                    knowledge_type,
                    clean_title,
                    clean_content,
                    confidence,
                    privacy,
                    now,
                    supersedes,
                    now,
                    now,
                ),
            )
            for evidence_id in dict.fromkeys(evidence_ids):
                connection.execute(
                    """
                    INSERT INTO evidence_links(
                        id, subject_id, evidence_id, relation_type, created_at
                    ) VALUES (?, ?, ?, 'supports', ?)
                    """,
                    (new_id("evl"), item_id, evidence_id, now),
                )
            self._audit(
                connection,
                "knowledge_candidate_created",
                "knowledge_item",
                item_id,
                {
                    "knowledge_type": knowledge_type,
                    "evidence_count": len(set(evidence_ids)),
                    "supersedes": supersedes,
                },
            )
            connection.commit()
        return {
            "id": item_id,
            "domain": domain,
            "knowledge_type": knowledge_type,
            "title": clean_title,
            "content": clean_content,
            "confidence": confidence,
            "review_status": "candidate",
            "privacy": privacy,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "supersedes": supersedes,
            "status": "created",
            "created_at": now,
        }

    def list_knowledge_items(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, domain, knowledge_type, title, content, confidence,
                       review_status, privacy, valid_from, valid_to, supersedes,
                       created_at, updated_at
                FROM knowledge_items
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_workflow_run(self, workflow_name: str, input_data: dict[str, Any]) -> str:
        run_id = new_id("wfr")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_runs(id, workflow_name, status, input_json, created_at)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (run_id, workflow_name, json.dumps(input_data, ensure_ascii=False), now),
            )
            connection.commit()
        return run_id

    def add_workflow_step(self, run_id: str, sequence: int, step_name: str) -> str:
        step_id = new_id("wfs")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_steps(
                    id, run_id, sequence, step_name, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (step_id, run_id, sequence, step_name, utc_now()),
            )
            connection.commit()
        return step_id

    def finish_workflow_step(
        self,
        step_id: str,
        *,
        status: str,
        summary: str,
        artifacts: list[str] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE workflow_steps
                SET status = ?, summary = ?, artifacts_json = ?, error_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    summary,
                    json.dumps(artifacts or [], ensure_ascii=False),
                    json.dumps(error, ensure_ascii=False) if error else None,
                    utc_now(),
                    step_id,
                ),
            )
            connection.commit()

    def finish_workflow_run(
        self,
        run_id: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE workflow_runs
                SET status = ?, output_json = ?, error_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(output, ensure_ascii=False) if output else None,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    utc_now(),
                    run_id,
                ),
            )
            connection.commit()

    def list_workflow_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, workflow_name, status, input_json, output_json,
                       error_json, created_at, completed_at
                FROM workflow_runs ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_agent_run(
        self,
        *,
        task: str,
        selected_domain: str | None,
        plan: list[str],
        context: dict[str, Any],
        result: dict[str, Any] | None,
        status: str = "completed",
    ) -> str:
        run_id = new_id("agr")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, task, selected_domain, status, plan_json,
                    context_json, result_json, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task,
                    selected_domain,
                    status,
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(context, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result else None,
                    now,
                    now if status == "completed" else None,
                ),
            )
            connection.commit()
        return run_id

    def create_agent_run(
        self,
        *,
        task: str,
        selected_domain: str | None,
        context: dict[str, Any] | None = None,
    ) -> str:
        run_id = new_id("agr")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, task, selected_domain, status, plan_json,
                    context_json, created_at
                ) VALUES (?, ?, ?, 'running', '[]', ?, ?)
                """,
                (
                    run_id,
                    task,
                    selected_domain,
                    json.dumps(context or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
            connection.commit()
        return run_id

    def finish_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        plan: list[str],
        context: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"completed", "warning", "failed"}:
            raise ValueError(f"不支持的 Agent 状态：{status}")
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, plan_json = ?, context_json = ?, result_json = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(context, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result else None,
                    utc_now(),
                    run_id,
                ),
            )
            connection.commit()

    def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, task, selected_domain, status, plan_json,
                       context_json, result_json, created_at, completed_at
                FROM agent_runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["plan"] = json.loads(item.pop("plan_json") or "[]")
        item["context"] = json.loads(item.pop("context_json") or "{}")
        result_json = item.pop("result_json")
        item["result"] = json.loads(result_json) if result_json else None
        return item

    def reopen_agent_run(self, run_id: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'running', completed_at = NULL
                WHERE id = ? AND status IN ('warning', 'failed')
                """,
                (run_id,),
            )
            connection.commit()
        return cursor.rowcount > 0

    def start_agent_step(
        self,
        *,
        run_id: str,
        sequence: int,
        phase: str,
        action_name: str,
        input_data: dict[str, Any] | None = None,
    ) -> str:
        now = utc_now()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM agent_steps WHERE run_id = ? AND sequence = ?",
                (run_id, sequence),
            ).fetchone()
            if existing is not None:
                step_id = existing["id"]
                connection.execute(
                    """
                    UPDATE agent_steps
                    SET phase = ?, action_name = ?, status = 'running', input_json = ?,
                        output_json = NULL, error_json = NULL, started_at = ?, completed_at = NULL
                    WHERE id = ?
                    """,
                    (
                        phase,
                        action_name,
                        json.dumps(input_data or {}, ensure_ascii=False),
                        now,
                        step_id,
                    ),
                )
                connection.commit()
                return step_id
            step_id = new_id("ags")
            connection.execute(
                """
                INSERT INTO agent_steps(
                    id, run_id, sequence, phase, action_name, status,
                    input_json, started_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    step_id,
                    run_id,
                    sequence,
                    phase,
                    action_name,
                    json.dumps(input_data or {}, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()
        return step_id

    def finish_agent_step(
        self,
        step_id: str,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"completed", "warning", "failed", "skipped"}:
            raise ValueError(f"不支持的 Agent 步骤状态：{status}")
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE agent_steps
                SET status = ?, output_json = ?, error_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(output, ensure_ascii=False) if output else None,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    utc_now(),
                    step_id,
                ),
            )
            connection.commit()

    def get_llm_cache(self, cache_key: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT response_json, usage_json FROM llm_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE llm_cache SET last_used_at = ? WHERE cache_key = ?",
                    (utc_now(), cache_key),
                )
                connection.commit()
        if not row:
            return None
        return {
            "response": json.loads(row["response_json"]),
            "usage": json.loads(row["usage_json"]),
        }

    def put_llm_cache(
        self,
        *,
        cache_key: str,
        model: str,
        prompt_version: str,
        response: dict[str, Any],
        usage: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_cache(
                    cache_key, model, prompt_version, response_json,
                    usage_json, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response_json = excluded.response_json,
                    usage_json = excluded.usage_json,
                    last_used_at = excluded.last_used_at
                """,
                (
                    cache_key,
                    model,
                    prompt_version,
                    json.dumps(response, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.commit()

    def record_llm_call(
        self,
        *,
        agent_run_id: str | None,
        task_type: str,
        request_hash: str,
        model: str,
        thinking_mode: str,
        status: str,
        usage: dict[str, Any] | None = None,
        estimated_cost_usd: float = 0.0,
        application_cache_hit: bool = False,
        error_code: str | None = None,
    ) -> str:
        usage = usage or {}
        call_id = new_id("llm")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_calls(
                    id, agent_run_id, task_type, request_hash, model,
                    thinking_mode, status, prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens, output_tokens, reasoning_tokens,
                    estimated_cost_usd, application_cache_hit, error_code,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    agent_run_id,
                    task_type,
                    request_hash,
                    model,
                    thinking_mode,
                    status,
                    int(usage.get("prompt_cache_hit_tokens", 0)),
                    int(usage.get("prompt_cache_miss_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                    int(usage.get("reasoning_tokens", 0)),
                    estimated_cost_usd,
                    int(application_cache_hit),
                    error_code,
                    now,
                    now,
                ),
            )
            connection.commit()
        return call_id

    def llm_usage_since(self, since: str) -> dict[str, float | int]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(prompt_cache_hit_tokens), 0) AS cache_hit_tokens,
                    COALESCE(SUM(prompt_cache_miss_tokens), 0) AS cache_miss_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
                FROM llm_calls
                WHERE created_at >= ?
                """,
                (since,),
            ).fetchone()
        return dict(row)

    def enqueue_agent_job(
        self,
        *,
        job_type: str,
        source_id: str | None,
        workspace_path: str | None,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        now = utc_now()
        job_id = new_id("agj")
        resolved_payload = payload or {}
        dedupe_key = str(resolved_payload.get("dedupe_key") or "").strip()
        with self.database.connect() as connection:
            existing = None
            if dedupe_key:
                existing = connection.execute(
                    """
                    SELECT * FROM agent_jobs
                    WHERE job_type = ?
                      AND json_extract(payload_json, '$.dedupe_key') = ?
                    """,
                    (job_type, dedupe_key),
                ).fetchone()
            elif source_id:
                existing = connection.execute(
                    "SELECT * FROM agent_jobs WHERE job_type = ? AND source_id = ?",
                    (job_type, source_id),
                ).fetchone()
            if existing:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO agent_jobs(
                    id, job_type, source_id, workspace_path, status, priority,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    source_id,
                    workspace_path,
                    priority,
                    json.dumps(resolved_payload, ensure_ascii=False),
                    now,
                ),
            )
            connection.commit()
        return {
            "id": job_id,
            "job_type": job_type,
            "source_id": source_id,
            "workspace_path": workspace_path,
            "status": "pending",
            "priority": priority,
            "payload": resolved_payload,
            "created_at": now,
        }

    def claim_agent_job(self, job_id: str | None = None) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if job_id:
                row = connection.execute(
                    """
                    SELECT * FROM agent_jobs
                    WHERE id = ? AND status = 'pending' AND attempts < max_attempts
                    """,
                    (job_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM agent_jobs
                    WHERE status = 'pending' AND attempts < max_attempts
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    """
                ).fetchone()
            if not row:
                connection.rollback()
                return None
            started_at = utc_now()
            connection.execute(
                """
                UPDATE agent_jobs
                SET status = 'running', attempts = attempts + 1, started_at = ?
                WHERE id = ?
                """,
                (started_at, row["id"]),
            )
            connection.commit()
        item = dict(row)
        item["attempts"] = int(item["attempts"]) + 1
        item["status"] = "running"
        item["started_at"] = started_at
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def defer_agent_job(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE agent_jobs
                SET status = 'pending',
                    attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    result_json = ?, error_json = ?, completed_at = NULL
                WHERE id = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False) if result else None,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    job_id,
                ),
            )
            connection.commit()

    def consolidate_pending_notify_closeouts(self) -> int:
        completed_at = utc_now()
        result = json.dumps(
            {"reason": "superseded_by_daily_closeout"},
            ensure_ascii=False,
        )
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_jobs
                SET status = 'skipped', result_json = ?, error_json = NULL,
                    completed_at = ?
                WHERE job_type = 'codex_closeout'
                  AND status = 'pending'
                  AND json_extract(payload_json, '$.capture_mode') = 'notify'
                """,
                (result, completed_at),
            )
            connection.commit()
        return int(cursor.rowcount)

    def consolidate_pending_legacy_daily_closeouts(self) -> int:
        """Retire per-thread daily jobs created before the bounded batch design."""
        completed_at = utc_now()
        result = json.dumps(
            {"reason": "superseded_by_daily_batch_v2"},
            ensure_ascii=False,
        )
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_jobs
                SET status = 'skipped', result_json = ?, error_json = NULL,
                    completed_at = ?
                WHERE job_type = 'codex_daily_closeout'
                  AND status = 'pending'
                  AND COALESCE(json_extract(payload_json, '$.capture_mode'), 'daily')
                      <> 'daily-batch-v2'
                """,
                (result, completed_at),
            )
            connection.commit()
        return int(cursor.rowcount)

    def finish_agent_job(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"completed", "failed", "skipped", "pending"}:
            raise ValueError(f"不支持的 Agent 任务状态：{status}")
        completed_at = None if status == "pending" else utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE agent_jobs
                SET status = ?, result_json = ?, error_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result else None,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    completed_at,
                    job_id,
                ),
            )
            connection.commit()

    def list_agent_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, job_type, source_id, workspace_path, status, priority,
                       attempts, max_attempts, payload_json, result_json, error_json,
                       created_at, started_at, completed_at
                FROM agent_jobs
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for source, target in (
                ("payload_json", "payload"),
                ("result_json", "result"),
                ("error_json", "error"),
            ):
                raw = item.pop(source)
                item[target] = json.loads(raw) if raw else None
            items.append(item)
        return items

    def source_document(self, source_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT s.id AS source_id, s.original_uri, s.original_name,
                       s.domain, s.privacy, s.source_type, s.metadata_json,
                       d.id AS document_id, d.title, d.text_content, d.created_at
                FROM sources s JOIN documents d ON d.source_id = s.id
                WHERE s.id = ?
                """,
                (source_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        self._annotate_evidence_status(item)
        return item

    def authorized_sync_root(self, path: str) -> dict[str, Any] | None:
        candidate = Path(path).expanduser().resolve(strict=False)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, root_uri, connector_type, domain, privacy, sync_mode
                FROM sync_roots WHERE enabled = 1
                """
            ).fetchall()
        matches: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            root = Path(row["root_uri"]).expanduser().resolve(strict=False)
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            matches.append((len(root.parts), dict(row)))
        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]

    def search_source_catalog(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        clean = query.strip()
        if not clean:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.id, i.root_id, r.name AS root_name, r.connector_type,
                       i.source_uri, i.relative_path, i.byte_size, i.modified_ns,
                       i.state, i.source_id, i.last_seen_at
                FROM sync_items i JOIN sync_roots r ON r.id = i.root_id
                WHERE i.state <> 'missing' AND i.relative_path LIKE ?
                ORDER BY i.modified_ns DESC
                LIMIT ?
                """,
                (f"%{clean}%", max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_agent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task, selected_domain, status, plan_json,
                       context_json, result_json, created_at, completed_at
                FROM agent_runs ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_persona_candidate(
        self,
        observation_type: str,
        statement: str,
        evidence_ids: list[str],
        confidence: str,
    ) -> dict[str, Any]:
        observation_id = new_id("per")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO persona_observations(
                    id, observation_type, statement, first_seen, last_seen,
                    evidence_count, evidence_json, confidence, approval_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                """,
                (
                    observation_id,
                    observation_type,
                    statement,
                    now,
                    now,
                    len(evidence_ids),
                    json.dumps(evidence_ids, ensure_ascii=False),
                    confidence,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                "persona_candidate_created",
                "persona_observation",
                observation_id,
                {"evidence_count": len(evidence_ids)},
            )
            connection.commit()
        return {
            "id": observation_id,
            "observation_type": observation_type,
            "statement": statement,
            "evidence_ids": evidence_ids,
            "confidence": confidence,
            "approval_status": "candidate",
            "created_at": now,
        }

    def list_persona_observations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM persona_observations
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["evidence_ids"] = json.loads(item.pop("evidence_json"))
            item["counterexamples"] = json.loads(item.pop("counterexamples_json"))
            results.append(item)
        return results

    def review_persona(self, observation_id: str, decision: str, reason: str) -> bool:
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT id FROM persona_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
            if not exists:
                return False
            connection.execute(
                """
                UPDATE persona_observations
                SET approval_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (decision, utc_now(), observation_id),
            )
            connection.execute(
                """
                INSERT INTO approval_records(
                    id, subject_type, subject_id, decision, reason, created_at
                ) VALUES (?, 'persona_observation', ?, ?, ?, ?)
                """,
                (new_id("apr"), observation_id, decision, reason, utc_now()),
            )
            self._audit(
                connection,
                "persona_reviewed",
                "persona_observation",
                observation_id,
                {"decision": decision, "reason": reason},
            )
            connection.commit()
        return True

    def create_distillation_candidate(
        self,
        *,
        example_type: str,
        input_text: str,
        preferred_output: str,
        rejected_output: str | None,
        rationale: str,
        source_ids: list[str],
        privacy: str,
    ) -> dict[str, Any]:
        example_id = new_id("dst")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO distillation_examples(
                    id, example_type, input_text, preferred_output,
                    rejected_output, rationale, source_ids_json, privacy,
                    approval_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                """,
                (
                    example_id,
                    example_type,
                    input_text,
                    preferred_output,
                    rejected_output,
                    rationale,
                    json.dumps(source_ids, ensure_ascii=False),
                    privacy,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                "distillation_candidate_created",
                "distillation_example",
                example_id,
                {"source_count": len(source_ids), "example_type": example_type},
            )
            connection.commit()
        return {
            "id": example_id,
            "example_type": example_type,
            "input_text": input_text,
            "preferred_output": preferred_output,
            "rejected_output": rejected_output,
            "rationale": rationale,
            "source_ids": source_ids,
            "privacy": privacy,
            "approval_status": "candidate",
            "created_at": now,
        }

    def list_distillation_examples(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM distillation_examples
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["source_ids"] = json.loads(item.pop("source_ids_json"))
            results.append(item)
        return results

    def review_distillation(self, example_id: str, decision: str, reason: str) -> bool:
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT id FROM distillation_examples WHERE id = ?",
                (example_id,),
            ).fetchone()
            if not exists:
                return False
            connection.execute(
                """
                UPDATE distillation_examples
                SET approval_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (decision, utc_now(), example_id),
            )
            connection.execute(
                """
                INSERT INTO approval_records(
                    id, subject_type, subject_id, decision, reason, created_at
                ) VALUES (?, 'distillation_example', ?, ?, ?, ?)
                """,
                (new_id("apr"), example_id, decision, reason, utc_now()),
            )
            self._audit(
                connection,
                "distillation_reviewed",
                "distillation_example",
                example_id,
                {"decision": decision, "reason": reason},
            )
            connection.commit()
        return True

    def approved_distillation_examples(
        self,
        *,
        approved_only: bool = True,
        dataset_split: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if approved_only:
            clauses.append("approval_status = 'approved'")
        if dataset_split:
            clauses.append("dataset_split = ?")
            params.append(dataset_split)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, example_type, input_text, preferred_output,
                       rejected_output, rationale, source_ids_json, privacy,
                       quality_score, approval_status, dataset_split
                FROM distillation_examples {where}
                ORDER BY created_at ASC
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["source_ids"] = json.loads(item.pop("source_ids_json"))
            results.append(item)
        return results

    def audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, subject_type, subject_id, details_json, created_at
                FROM audit_events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            results.append(item)
        return results
