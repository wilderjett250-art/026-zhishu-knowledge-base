import hashlib
import json
import sqlite3
from typing import Any

from pkas.db import Database
from pkas.repository import new_id, utc_now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _timestamp(value: Any) -> int:
    try:
        timestamp = int(float(value))
    except (TypeError, ValueError):
        return 0
    return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp


class CustomerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def upsert_connector(
        self,
        *,
        connector_type: str,
        name: str,
        base_url: str | None,
        status: str,
        config: dict[str, Any] | None = None,
        healthy: bool = False,
    ) -> str:
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM connectors WHERE connector_type = ? AND base_url IS ?",
                (connector_type, base_url),
            ).fetchone()
            connector_id = row["id"] if row else new_id("con")
            connection.execute(
                """
                INSERT INTO connectors(
                    id, connector_type, name, base_url, status, config_json,
                    last_health_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_type, base_url) DO UPDATE SET
                    name = excluded.name,
                    status = excluded.status,
                    config_json = excluded.config_json,
                    last_health_at = CASE
                        WHEN excluded.last_health_at IS NOT NULL
                        THEN excluded.last_health_at ELSE connectors.last_health_at END,
                    updated_at = excluded.updated_at
                """,
                (
                    connector_id,
                    connector_type,
                    name,
                    base_url,
                    status,
                    _json(config or {}),
                    now if healthy else None,
                    now,
                    now,
                ),
            )
            connection.commit()
        return connector_id

    def ingest_chatlab_page(
        self,
        *,
        payload: dict[str, Any],
        session_id: str,
        connector_id: str,
        snapshot: dict[str, Any],
        privacy: str,
        contact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_meta = payload.get("meta")
        raw_messages = payload.get("messages")
        raw_members = payload.get("members")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        messages: list[Any] = raw_messages if isinstance(raw_messages, list) else []
        members: list[Any] = raw_members if isinstance(raw_members, list) else []
        contact = contact or {}
        now = utc_now()
        display_name = _text(
            contact.get("remark") or contact.get("displayName") or meta.get("name") or session_id
        )
        default_type = "group" if session_id.endswith("@chatroom") else "private"
        customer_type = _text(meta.get("type"), default_type)
        owner_platform_id = _text(meta.get("ownerId")) or None

        with self.database.connect() as connection:
            customer_id = self._upsert_customer(
                connection,
                session_id=session_id,
                display_name=display_name,
                customer_type=customer_type,
                privacy=privacy,
                contact=contact,
                metadata={"weflow_meta": meta},
                now=now,
            )
            conversation_id = self._upsert_conversation(
                connection,
                customer_id=customer_id,
                connector_id=connector_id,
                session_id=session_id,
                display_name=display_name,
                conversation_type=customer_type,
                owner_platform_id=owner_platform_id,
                privacy=privacy,
                metadata={"chatlab": payload.get("chatlab", {})},
                now=now,
            )
            snapshot_id = self._upsert_snapshot(
                connection,
                connector_id=connector_id,
                conversation_id=conversation_id,
                snapshot=snapshot,
                now=now,
            )
            self._upsert_members(connection, conversation_id, members, now)
            imported = 0
            duplicates = 0
            for message in messages:
                if not isinstance(message, dict):
                    continue
                added = self._insert_message(
                    connection,
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                    snapshot_id=snapshot_id,
                    message=message,
                    owner_platform_id=owner_platform_id,
                    privacy=privacy,
                    now=now,
                )
                imported += int(added)
                duplicates += int(not added)

            summary = connection.execute(
                """
                SELECT COUNT(*) AS message_count,
                       MIN(sent_at) AS first_message_at,
                       MAX(sent_at) AS last_message_at
                FROM customer_messages WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE customer_conversations
                SET message_count = ?, first_message_at = ?, last_message_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    summary["message_count"],
                    summary["first_message_at"],
                    summary["last_message_at"],
                    now,
                    conversation_id,
                ),
            )
            connection.execute(
                """
                UPDATE customers SET last_message_at = ?, updated_at = ? WHERE id = ?
                """,
                (summary["last_message_at"], now, customer_id),
            )
            self._audit(
                connection,
                "weflow_messages_ingested",
                "customer_conversation",
                conversation_id,
                {
                    "session_id": session_id,
                    "imported": imported,
                    "duplicates": duplicates,
                    "snapshot_id": snapshot_id,
                },
            )
            connection.commit()
        return {
            "customer_id": customer_id,
            "conversation_id": conversation_id,
            "snapshot_id": snapshot_id,
            "imported": imported,
            "duplicates": duplicates,
            "message_count": summary["message_count"],
            "last_message_at": summary["last_message_at"],
        }

    @staticmethod
    def _upsert_customer(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        display_name: str,
        customer_type: str,
        privacy: str,
        contact: dict[str, Any],
        metadata: dict[str, Any],
        now: str,
    ) -> str:
        row = connection.execute(
            "SELECT id FROM customers WHERE platform = 'wechat' AND platform_id = ?",
            (session_id,),
        ).fetchone()
        customer_id = row["id"] if row else new_id("cus")
        connection.execute(
            """
            INSERT INTO customers(
                id, platform, platform_id, display_name, customer_type,
                remark, nickname, alias, privacy, metadata_json,
                created_at, updated_at
            ) VALUES (?, 'wechat', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, platform_id) DO UPDATE SET
                display_name = excluded.display_name,
                customer_type = excluded.customer_type,
                remark = COALESCE(NULLIF(excluded.remark, ''), customers.remark),
                nickname = COALESCE(NULLIF(excluded.nickname, ''), customers.nickname),
                alias = COALESCE(NULLIF(excluded.alias, ''), customers.alias),
                privacy = excluded.privacy,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                customer_id,
                session_id,
                display_name,
                customer_type,
                _text(contact.get("remark")) or None,
                _text(contact.get("nickname")) or None,
                _text(contact.get("alias")) or None,
                privacy,
                _json(metadata),
                now,
                now,
            ),
        )
        return customer_id

    @staticmethod
    def _upsert_conversation(
        connection: sqlite3.Connection,
        *,
        customer_id: str,
        connector_id: str,
        session_id: str,
        display_name: str,
        conversation_type: str,
        owner_platform_id: str | None,
        privacy: str,
        metadata: dict[str, Any],
        now: str,
    ) -> str:
        row = connection.execute(
            """
            SELECT id FROM customer_conversations
            WHERE platform = 'wechat' AND platform_id = ?
            """,
            (session_id,),
        ).fetchone()
        conversation_id = row["id"] if row else new_id("cvc")
        connection.execute(
            """
            INSERT INTO customer_conversations(
                id, customer_id, connector_id, platform, platform_id,
                name, conversation_type, owner_platform_id, privacy,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'wechat', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, platform_id) DO UPDATE SET
                customer_id = excluded.customer_id,
                connector_id = excluded.connector_id,
                name = excluded.name,
                conversation_type = excluded.conversation_type,
                owner_platform_id = COALESCE(excluded.owner_platform_id,
                                             customer_conversations.owner_platform_id),
                privacy = excluded.privacy,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                conversation_id,
                customer_id,
                connector_id,
                session_id,
                display_name,
                conversation_type,
                owner_platform_id,
                privacy,
                _json(metadata),
                now,
                now,
            ),
        )
        return conversation_id

    @staticmethod
    def _upsert_snapshot(
        connection: sqlite3.Connection,
        *,
        connector_id: str,
        conversation_id: str,
        snapshot: dict[str, Any],
        now: str,
    ) -> str:
        row = connection.execute(
            "SELECT id FROM connector_snapshots WHERE content_hash = ?",
            (snapshot["content_hash"],),
        ).fetchone()
        if row:
            return row["id"]
        snapshot_id = new_id("snp")
        connection.execute(
            """
            INSERT INTO connector_snapshots(
                id, connector_id, conversation_id, content_hash, vault_path,
                byte_size, source_uri, captured_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                connector_id,
                conversation_id,
                snapshot["content_hash"],
                snapshot["vault_path"],
                snapshot["byte_size"],
                snapshot["source_uri"],
                now,
                _json(snapshot.get("metadata", {})),
            ),
        )
        return snapshot_id

    @staticmethod
    def _upsert_members(
        connection: sqlite3.Connection,
        conversation_id: str,
        members: list[Any],
        now: str,
    ) -> None:
        for member in members:
            if not isinstance(member, dict):
                continue
            platform_id = _text(member.get("platformId") or member.get("wxid"))
            if not platform_id:
                continue
            connection.execute(
                """
                INSERT INTO customer_members(
                    id, conversation_id, platform_id, account_name,
                    group_nickname, avatar_url, role, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, platform_id) DO UPDATE SET
                    account_name = excluded.account_name,
                    group_nickname = excluded.group_nickname,
                    avatar_url = excluded.avatar_url,
                    role = excluded.role,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    new_id("mem"),
                    conversation_id,
                    platform_id,
                    _text(member.get("accountName") or member.get("displayName")) or None,
                    _text(member.get("groupNickname")) or None,
                    _text(member.get("avatar") or member.get("avatarUrl")) or None,
                    "owner" if member.get("isOwner") else None,
                    _json(member),
                    now,
                    now,
                ),
            )

    @staticmethod
    def _insert_message(
        connection: sqlite3.Connection,
        *,
        customer_id: str,
        conversation_id: str,
        snapshot_id: str,
        message: dict[str, Any],
        owner_platform_id: str | None,
        privacy: str,
        now: str,
    ) -> bool:
        sender_id = _text(message.get("sender") or message.get("senderUsername")) or None
        sender_name = (
            _text(message.get("groupNickname") or message.get("accountName") or sender_id) or None
        )
        sent_at = _timestamp(message.get("timestamp") or message.get("createTime"))
        content = _text(
            message.get("content") or message.get("parsedContent") or message.get("rawContent")
        ).strip()
        if not content:
            content = "[空消息]"
        platform_message_id = (
            _text(message.get("platformMessageId") or message.get("serverId")) or None
        )
        local_id = _text(message.get("localId")) or None
        message_type_value = message.get("type")
        if message_type_value is None:
            message_type_value = message.get("localType")
        message_type = _text(message_type_value, "unknown")
        sent_by_self = bool(message.get("isSend")) or bool(
            owner_platform_id and sender_id == owner_platform_id
        )
        identity = {
            "conversation_id": conversation_id,
            "platform_message_id": platform_message_id,
            "local_id": local_id,
            "sender": sender_id,
            "timestamp": sent_at,
            "type": message_type,
            "content": content,
        }
        dedup_identity = message.get("dedupKey")
        if not isinstance(dedup_identity, str) or not dedup_identity:
            dedup_identity = _json(identity)
        source_hash = hashlib.sha256(dedup_identity.encode("utf-8")).hexdigest()
        exists = connection.execute(
            """
            SELECT id FROM customer_messages
            WHERE conversation_id = ? AND source_hash = ?
            """,
            (conversation_id, source_hash),
        ).fetchone()
        if exists:
            return False
        message_id = new_id("cmsg")
        media = {
            key: message.get(key)
            for key in ("mediaPath", "mediaType", "mediaFileName", "mediaLocalPath")
            if message.get(key) is not None
        }
        connection.execute(
            """
            INSERT INTO customer_messages(
                id, conversation_id, snapshot_id, platform_message_id,
                local_id, sender_platform_id, sender_name, is_self, sent_at,
                message_type, content, raw_content, parsed_content,
                reply_to_message_id, quote_json, media_json, source_hash,
                privacy, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                snapshot_id,
                platform_message_id,
                local_id,
                sender_id,
                sender_name,
                int(sent_by_self),
                sent_at,
                message_type,
                content,
                _text(message.get("rawContent")) or None,
                _text(message.get("parsedContent")) or None,
                _text(message.get("replyToMessageId")) or None,
                _json(message.get("quote")) if message.get("quote") else None,
                _json(media) if media else None,
                source_hash,
                privacy,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO customer_messages_fts(
                message_id, customer_id, conversation_id,
                sender_name, content, privacy
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, customer_id, conversation_id, sender_name, content, privacy),
        )
        return True

    def get_sync_cursor(self, session_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT sync_since, sync_offset, sync_watermark
                FROM customer_conversations
                WHERE platform = 'wechat' AND platform_id = ?
                """,
                (session_id,),
            ).fetchone()
        if not row:
            return {"since": 0, "offset": 0, "watermark": 0}
        return {
            "since": row["sync_since"],
            "offset": row["sync_offset"],
            "watermark": row["sync_watermark"],
        }

    def update_sync_cursor(
        self,
        session_id: str,
        *,
        since: int,
        offset: int,
        watermark: int,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE customer_conversations
                SET sync_since = ?, sync_offset = ?, sync_watermark = ?,
                    last_synced_at = ?, updated_at = ?
                WHERE platform = 'wechat' AND platform_id = ?
                """,
                (since, offset, watermark, utc_now(), utc_now(), session_id),
            )
            connection.commit()

    def list_customers(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*,
                       COALESCE(cv.message_count, 0) AS message_count,
                       cv.id AS conversation_id,
                       cv.last_synced_at,
                       (SELECT COUNT(*) FROM customer_signals s
                        WHERE s.customer_id = c.id
                          AND s.approval_status <> 'rejected'
                          AND s.status = 'open') AS open_signals
                FROM customers c
                LEFT JOIN customer_conversations cv ON cv.customer_id = c.id
                ORDER BY c.last_message_at DESC, c.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._decode_customer(dict(row)) for row in rows]

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT c.*, cv.id AS conversation_id, cv.name AS conversation_name,
                       cv.conversation_type, cv.message_count, cv.first_message_at,
                       cv.last_message_at AS conversation_last_message_at,
                       cv.last_synced_at, cv.privacy AS conversation_privacy
                FROM customers c
                LEFT JOIN customer_conversations cv ON cv.customer_id = c.id
                WHERE c.id = ?
                """,
                (customer_id,),
            ).fetchone()
        return self._decode_customer(dict(row)) if row else None

    def update_customer(
        self,
        customer_id: str,
        *,
        company: str | None = None,
        stage: str | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        review_status: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_customer(customer_id)
        if not current:
            return None
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE customers SET company = ?, stage = ?, tags_json = ?,
                    summary = ?, review_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    company if company is not None else current.get("company"),
                    stage if stage is not None else current.get("stage"),
                    _json(tags if tags is not None else current.get("tags", [])),
                    summary if summary is not None else current.get("summary"),
                    review_status if review_status is not None else current.get("review_status"),
                    utc_now(),
                    customer_id,
                ),
            )
            self._audit(
                connection,
                "customer_profile_updated",
                "customer",
                customer_id,
                {
                    "fields": [
                        key
                        for key, value in {
                            "company": company,
                            "stage": stage,
                            "tags": tags,
                            "summary": summary,
                            "review_status": review_status,
                        }.items()
                        if value is not None
                    ]
                },
            )
            connection.commit()
        return self.get_customer(customer_id)

    @staticmethod
    def _decode_customer(item: dict[str, Any]) -> dict[str, Any]:
        item["tags"] = json.loads(item.pop("tags_json", "[]"))
        item["metadata"] = json.loads(item.pop("metadata_json", "{}"))
        return item

    def timeline(
        self,
        customer_id: str,
        *,
        limit: int = 100,
        before: int | None = None,
        include_restricted: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["cv.customer_id = ?"]
        params: list[Any] = [customer_id]
        if before:
            clauses.append("m.sent_at < ?")
            params.append(before)
        if not include_restricted:
            clauses.append("m.privacy <> 'restricted'")
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT m.*, cv.name AS conversation_name,
                       s.source_uri, s.vault_path, s.content_hash AS snapshot_hash
                FROM customer_messages m
                JOIN customer_conversations cv ON cv.id = m.conversation_id
                LEFT JOIN connector_snapshots s ON s.id = m.snapshot_id
                WHERE {" AND ".join(clauses)}
                ORDER BY m.sent_at DESC, m.id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._decode_message(dict(row)) for row in rows]

    @staticmethod
    def _decode_message(item: dict[str, Any]) -> dict[str, Any]:
        item["quote"] = json.loads(item.pop("quote_json")) if item.get("quote_json") else None
        item["media"] = json.loads(item.pop("media_json")) if item.get("media_json") else None
        return item

    @staticmethod
    def _fts_expression(query: str) -> str:
        tokens = [token.strip() for token in query.split() if token.strip()]
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search_messages(
        self,
        query: str,
        *,
        customer_id: str | None = None,
        limit: int = 20,
        include_restricted: bool = False,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        clauses = []
        params: list[Any] = [self._fts_expression(query)]
        if customer_id:
            clauses.append("c.id = ?")
            params.append(customer_id)
        if not include_restricted:
            clauses.append("m.privacy <> 'restricted'")
        extra = "".join(f" AND {clause}" for clause in clauses)
        params.append(limit)
        try:
            with self.database.connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT m.id AS message_id, m.conversation_id,
                           m.platform_message_id, m.sender_name, m.is_self,
                           m.sent_at, m.message_type, m.privacy,
                           c.id AS customer_id, c.display_name AS customer_name,
                           cv.name AS conversation_name,
                           s.source_uri, s.vault_path,
                           snippet(customer_messages_fts, 4, '', '', ' … ', 42) AS snippet,
                           bm25(customer_messages_fts, 0.0, 0.0, 0.0, 2.0, 5.0, 0.0) AS score
                    FROM customer_messages_fts
                    JOIN customer_messages m ON m.id = customer_messages_fts.message_id
                    JOIN customer_conversations cv ON cv.id = m.conversation_id
                    JOIN customers c ON c.id = cv.customer_id
                    LEFT JOIN connector_snapshots s ON s.id = m.snapshot_id
                    WHERE customer_messages_fts MATCH ? {extra}
                    ORDER BY score LIMIT ?
                    """,
                    params,
                ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            return [dict(row) for row in rows]

        clauses = ["m.content LIKE ?"]
        fallback_params: list[Any] = [f"%{query}%"]
        if customer_id:
            clauses.append("c.id = ?")
            fallback_params.append(customer_id)
        if not include_restricted:
            clauses.append("m.privacy <> 'restricted'")
        fallback_params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT m.id AS message_id, m.conversation_id,
                       m.platform_message_id, m.sender_name, m.is_self,
                       m.sent_at, m.message_type, m.privacy,
                       c.id AS customer_id, c.display_name AS customer_name,
                       cv.name AS conversation_name,
                       s.source_uri, s.vault_path,
                       m.content AS snippet, 0.0 AS score
                FROM customer_messages m
                JOIN customer_conversations cv ON cv.id = m.conversation_id
                JOIN customers c ON c.id = cv.customer_id
                LEFT JOIN connector_snapshots s ON s.id = m.snapshot_id
                WHERE {" AND ".join(clauses)}
                ORDER BY m.sent_at DESC LIMIT ?
                """,
                fallback_params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_signal(
        self,
        *,
        customer_id: str,
        signal_type: str,
        statement: str,
        status: str,
        due_at: str | None,
        evidence_message_ids: list[str],
        confidence: str,
    ) -> dict[str, Any] | None:
        if not self.get_customer(customer_id):
            return None
        signal_id = new_id("sig")
        now = utc_now()
        with self.database.connect() as connection:
            conversation = connection.execute(
                "SELECT id FROM customer_conversations WHERE customer_id = ? LIMIT 1",
                (customer_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO customer_signals(
                    id, customer_id, conversation_id, signal_type, statement,
                    status, due_at, evidence_message_ids_json, confidence,
                    approval_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                """,
                (
                    signal_id,
                    customer_id,
                    conversation["id"] if conversation else None,
                    signal_type,
                    statement,
                    status,
                    due_at,
                    _json(evidence_message_ids),
                    confidence,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                "customer_signal_created",
                "customer_signal",
                signal_id,
                {"customer_id": customer_id, "signal_type": signal_type},
            )
            connection.commit()
        return {
            "id": signal_id,
            "customer_id": customer_id,
            "signal_type": signal_type,
            "statement": statement,
            "status": status,
            "due_at": due_at,
            "evidence_message_ids": evidence_message_ids,
            "confidence": confidence,
            "approval_status": "candidate",
            "created_at": now,
        }

    def list_signals(
        self,
        customer_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where = "WHERE s.customer_id = ?" if customer_id else ""
        params: list[Any] = [customer_id] if customer_id else []
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT s.*, c.display_name AS customer_name
                FROM customer_signals s JOIN customers c ON c.id = s.customer_id
                {where} ORDER BY s.updated_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["evidence_message_ids"] = json.loads(item.pop("evidence_message_ids_json"))
            results.append(item)
        return results

    def review_signal(self, signal_id: str, decision: str, reason: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM customer_signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
            if not row:
                return False
            now = utc_now()
            connection.execute(
                "UPDATE customer_signals SET approval_status = ?, updated_at = ? WHERE id = ?",
                (decision, now, signal_id),
            )
            connection.execute(
                """
                INSERT INTO approval_records(
                    id, subject_type, subject_id, decision, reason, created_at
                ) VALUES (?, 'customer_signal', ?, ?, ?, ?)
                """,
                (new_id("apr"), signal_id, decision, reason, now),
            )
            self._audit(
                connection,
                "customer_signal_reviewed",
                "customer_signal",
                signal_id,
                {"decision": decision, "reason": reason},
            )
            connection.commit()
        return True

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        event_type: str,
        subject_type: str,
        subject_id: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(
                event_type, subject_type, subject_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, subject_type, subject_id, _json(details), utc_now()),
        )
