import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from pkas.db import Database
from pkas.parsers import ParsedDocument


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Repository:
    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()
        self.database.initialize()

    def source_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        return dict(row) if row else None

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
                            document_id,
                            source_id,
                            chunk["sequence"],
                            parsed.title,
                            chunk["text"],
                            chunk["locator"],
                            domain,
                            privacy,
                            len(chunk["text"]),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO chunks_fts(chunk_id, title, content, domain, privacy)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (chunk_id, parsed.title, chunk["text"], domain, privacy),
                    )
                self._audit(
                    connection,
                    "source_ingested",
                    "source",
                    source_id,
                    {"document_id": document_id, "chunk_count": len(chunks)},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "source_id": source_id,
            "document_id": document_id,
            "chunk_count": len(chunks),
            "message_count": len(parsed.messages),
        }

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
            ):
                counts[table] = connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
            domains = {
                row["domain"]: row["count"]
                for row in connection.execute(
                    "SELECT domain, COUNT(*) AS count FROM sources GROUP BY domain"
                ).fetchall()
            }
            recent_sources = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, original_name, source_type, domain, privacy, ingested_at
                    FROM sources ORDER BY ingested_at DESC LIMIT 8
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

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, original_uri, original_name, vault_path, source_type,
                       content_hash, byte_size, mime_type, domain, privacy,
                       status, created_at, ingested_at
                FROM sources ORDER BY ingested_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _fts_expression(query: str) -> str:
        tokens = [token.strip() for token in query.split() if token.strip()]
        if not tokens:
            return '""'
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 10,
        include_restricted: bool = False,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        clauses = []
        params: list[Any] = [self._fts_expression(query)]
        if domain:
            clauses.append("c.domain = ?")
            params.append(domain)
        if not include_restricted:
            clauses.append("c.privacy <> 'restricted'")
        where_extra = "".join(f" AND {clause}" for clause in clauses)
        params.append(limit)
        statement = f"""
            SELECT c.id AS chunk_id, c.document_id, c.source_id, c.title,
                   c.locator, c.domain, c.privacy, s.original_uri, s.vault_path,
                   snippet(chunks_fts, 2, '', '', ' … ', 36) AS snippet,
                   bm25(chunks_fts, 0.0, 5.0, 1.0, 0.0, 0.0) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            JOIN sources s ON s.id = c.source_id
            WHERE chunks_fts MATCH ? {where_extra}
            ORDER BY score
            LIMIT ?
        """
        try:
            with self.database.connect() as connection:
                rows = connection.execute(statement, params).fetchall()
        except sqlite3.OperationalError:
            rows = []

        if rows:
            return [dict(row) for row in rows]

        like_clauses = ["c.text_content LIKE ?"]
        like_params: list[Any] = [f"%{query}%"]
        if domain:
            like_clauses.append("c.domain = ?")
            like_params.append(domain)
        if not include_restricted:
            like_clauses.append("c.privacy <> 'restricted'")
        like_params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, c.document_id, c.source_id, c.title,
                       c.locator, c.domain, c.privacy, s.original_uri, s.vault_path,
                       substr(c.text_content, 1, 500) AS snippet, 0.0 AS score
                FROM chunks c
                JOIN sources s ON s.id = c.source_id
                WHERE {" AND ".join(like_clauses)}
                ORDER BY c.created_at DESC
                LIMIT ?
                """,
                like_params,
            ).fetchall()
        return [dict(row) for row in rows]

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
                       s.id AS source_id, s.original_uri, s.vault_path, s.domain, s.privacy
                FROM documents d JOIN sources s ON s.id = d.source_id
                WHERE d.id = ?
                """,
                (document_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        text = data.pop("text_content")
        data["text"] = text[offset : offset + limit]
        data["offset"] = offset
        data["returned_chars"] = len(data["text"])
        data["total_chars"] = len(text)
        data["has_more"] = offset + limit < len(text)
        return data

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
