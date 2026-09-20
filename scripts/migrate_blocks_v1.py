"""Backfill Block relations and re-chunk active non-Codex sources with typed-v1."""

import hashlib
import json
from pathlib import Path

from pkas.ingest import CHUNKER_VERSION, chunk_document
from pkas.parsers import ParseError, parse_file
from pkas.repository import Repository, utc_now
from pkas.system import KnowledgeSystem

MIGRATION_KEY = "block_child_chunk_model_v1"


def legacy_block_id(chunk_id: str) -> str:
    return "blk_" + hashlib.sha256(f"legacy:{chunk_id}".encode()).hexdigest()[:32]


def backfill_legacy(repository: Repository) -> dict[str, int]:
    inserted = 0
    linked = 0
    with repository.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT c.id AS chunk_id, c.document_id, c.sequence, c.text_content,
                   c.locator, c.char_count, c.created_at
            FROM chunks c
            LEFT JOIN chunk_blocks cb ON cb.chunk_id=c.id
            WHERE cb.chunk_id IS NULL
            ORDER BY c.document_id, c.sequence
            """
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            block_id = legacy_block_id(str(row["chunk_id"]))
            connection.execute(
                """
                INSERT OR IGNORE INTO blocks(
                    id, document_id, sequence, parent_id, kind, text_content,
                    locator, page, bbox_json, metadata_json, char_count,
                    content_hash, created_at
                ) VALUES (?, ?, ?, NULL, 'legacy-chunk', ?, ?, NULL, NULL, '{}', ?, ?, ?)
                """,
                (
                    block_id,
                    row["document_id"],
                    row["sequence"],
                    row["text_content"],
                    row["locator"],
                    row["char_count"],
                    hashlib.sha256(str(row["text_content"]).encode()).hexdigest(),
                    row["created_at"] or utc_now(),
                ),
            )
            connection.execute(
                """INSERT OR IGNORE INTO chunk_blocks(chunk_id, block_id, sequence, is_primary)
                VALUES (?, ?, 0, 1)""",
                (row["chunk_id"], block_id),
            )
            connection.execute(
                """UPDATE chunks SET block_id=?, chunk_kind=COALESCE(chunk_kind, 'legacy'),
                chunker_version=COALESCE(chunker_version, 'legacy-v1') WHERE id=?""",
                (block_id, row["chunk_id"]),
            )
            inserted += int(connection.execute("SELECT changes()").fetchone()[0] > 0)
            linked += 1
            if index % 2000 == 0:
                connection.commit()
        connection.commit()
    return {"legacy_blocks_inserted": inserted, "legacy_chunks_linked": linked}


def reindex_non_codex(system: KnowledgeSystem) -> dict[str, int]:
    with system.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT s.id, s.vault_path, s.privacy
            FROM sources s JOIN documents d ON d.source_id=s.id
            WHERE s.status='indexed' AND s.source_type<>'codex-turn'
              AND NOT EXISTS(
                  SELECT 1 FROM chunks c
                  WHERE c.source_id=s.id AND c.chunker_version=?
              )
            ORDER BY s.ingested_at
            """,
            (CHUNKER_VERSION,),
        ).fetchall()
    reindexed = 0
    errors = 0
    for row in rows:
        try:
            parsed = parse_file(
                Path(row["vault_path"]),
                settings=system.settings,
                privacy=str(row["privacy"]),
            )
            chunks = chunk_document(parsed)
            if not chunks:
                raise ParseError("没有可索引片段。")
            system.repository.replace_document(
                source_id=str(row["id"]),
                parsed=parsed,
                chunks=chunks,
                preserve_ingested_at=True,
            )
            reindexed += 1
        except (OSError, ParseError, ValueError):
            errors += 1
    return {"candidates": len(rows), "reindexed": reindexed, "errors": errors}


def validate(system: KnowledgeSystem) -> dict[str, int]:
    with system.database.connect() as connection:
        counts = {
            "active_chunks": connection.execute(
                """SELECT COUNT(*) FROM chunks c JOIN sources s ON s.id=c.source_id
                WHERE s.status='indexed'"""
            ).fetchone()[0],
            "orphan_chunks": connection.execute(
                """SELECT COUNT(*) FROM chunks c JOIN sources s ON s.id=c.source_id
                LEFT JOIN chunk_blocks cb ON cb.chunk_id=c.id
                LEFT JOIN blocks b ON b.id=cb.block_id
                WHERE s.status='indexed' AND (cb.block_id IS NULL OR b.id IS NULL)"""
            ).fetchone()[0],
            "active_blocks": connection.execute(
                """SELECT COUNT(*) FROM blocks b JOIN documents d ON d.id=b.document_id
                JOIN sources s ON s.id=d.source_id WHERE s.status='indexed'"""
            ).fetchone()[0],
            "typed_non_codex_chunks": connection.execute(
                """SELECT COUNT(*) FROM chunks c JOIN sources s ON s.id=c.source_id
                WHERE s.status='indexed' AND s.source_type<>'codex-turn'
                  AND c.chunker_version=?""",
                (CHUNKER_VERSION,),
            ).fetchone()[0],
            "legacy_non_codex_chunks": connection.execute(
                """SELECT COUNT(*) FROM chunks c JOIN sources s ON s.id=c.source_id
                WHERE s.status='indexed' AND s.source_type<>'codex-turn'
                  AND c.chunker_version<>?""",
                (CHUNKER_VERSION,),
            ).fetchone()[0],
        }
    return {key: int(value) for key, value in counts.items()}


def main() -> None:
    system = KnowledgeSystem.create()
    system.database.initialize()
    if system.repository.get_app_meta(MIGRATION_KEY) == "completed":
        print(json.dumps({"status": "already_completed", **validate(system)}))
        return
    legacy = backfill_legacy(system.repository)
    typed = reindex_non_codex(system)
    checks = validate(system)
    status = "completed" if typed["errors"] == 0 and checks["orphan_chunks"] == 0 else "warning"
    if status == "completed":
        system.repository.set_app_meta(MIGRATION_KEY, "completed")
    print(json.dumps({"status": status, **legacy, **typed, **checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
