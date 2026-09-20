from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "index" / "pkas.sqlite"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "backups"

CODEX_ONLY_KNOWLEDGE_FILTER = """
EXISTS (
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


def backup(database_path: Path, output_dir: Path) -> dict[str, object]:
    database = database_path.resolve(strict=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output_dir / f"pkas-codex-user-task-migration-rollback-{timestamp}.sqlite"
    if output.exists():
        raise FileExistsError(f"Rollback backup already exists: {output}")

    source_uri = f"file:{database.as_posix()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    try:
        source.execute("PRAGMA busy_timeout = 30000")
        source.execute("ATTACH DATABASE ? AS rollback", (str(output),))
        source.execute("BEGIN")
        source.execute(
            """
            CREATE TABLE rollback.codex_sources AS
            SELECT * FROM main.sources WHERE source_type = 'codex-turn'
            """
        )
        source.execute(
            """
            CREATE TABLE rollback.codex_documents AS
            SELECT d.* FROM main.documents d
            JOIN main.sources s ON s.id = d.source_id
            WHERE s.source_type = 'codex-turn'
            """
        )
        source.execute(
            """
            CREATE TABLE rollback.codex_chunks AS
            SELECT c.* FROM main.chunks c
            JOIN main.sources s ON s.id = c.source_id
            WHERE s.source_type = 'codex-turn'
            """
        )
        source.execute(
            """
            CREATE TABLE rollback.codex_chunks_fts AS
            SELECT f.chunk_id, f.title, f.content, f.domain, f.privacy
            FROM main.chunks_fts f
            JOIN main.chunks c ON c.id = f.chunk_id
            JOIN main.sources s ON s.id = c.source_id
            WHERE s.source_type = 'codex-turn'
            """
        )
        source.execute(
            """
            CREATE TABLE rollback.codex_messages AS
            SELECT m.* FROM main.messages m
            JOIN main.documents d ON d.id = m.document_id
            JOIN main.sources s ON s.id = d.source_id
            WHERE s.source_type = 'codex-turn'
            """
        )
        source.execute(
            f"""
            CREATE TABLE rollback.codex_knowledge_items AS
            SELECT k.* FROM main.knowledge_items k
            WHERE {CODEX_ONLY_KNOWLEDGE_FILTER}
            """
        )
        source.execute(
            """
            CREATE TABLE rollback.codex_evidence_links AS
            SELECT e.* FROM main.evidence_links e
            JOIN rollback.codex_knowledge_items k ON k.id = e.subject_id
            """
        )
        source.execute(
            """
            CREATE TABLE rollback.migration_app_meta AS
            SELECT * FROM main.app_meta
            WHERE key LIKE 'codex_turn_user_task_only_v1%'
            """
        )
        source.execute(
            """
            CREATE TABLE rollback.backup_manifest (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "source_database": str(database),
            "purpose": "rollback-before-codex-user-task-only-v1",
        }
        source.executemany(
            "INSERT INTO rollback.backup_manifest(key, value) VALUES(?, ?)",
            manifest.items(),
        )
        source.commit()
        source.execute("DETACH DATABASE rollback")
    except Exception:
        source.rollback()
        source.close()
        output.unlink(missing_ok=True)
        raise
    finally:
        with suppress(sqlite3.Error):
            source.close()

    verification = sqlite3.connect(output)
    try:
        quick_check = str(verification.execute("PRAGMA quick_check").fetchone()[0])
        counts = {
            "sources": verification.execute(
                "SELECT count(*) FROM codex_sources"
            ).fetchone()[0],
            "documents": verification.execute(
                "SELECT count(*) FROM codex_documents"
            ).fetchone()[0],
            "chunks": verification.execute(
                "SELECT count(*) FROM codex_chunks"
            ).fetchone()[0],
            "fts_rows": verification.execute(
                "SELECT count(*) FROM codex_chunks_fts"
            ).fetchone()[0],
            "knowledge_items": verification.execute(
                "SELECT count(*) FROM codex_knowledge_items"
            ).fetchone()[0],
        }
    finally:
        verification.close()
    if quick_check != "ok":
        raise RuntimeError(f"Rollback backup quick_check failed: {quick_check}")
    return {
        "status": "completed",
        "backup_path": str(output),
        "byte_size": output.stat().st_size,
        "quick_check": quick_check,
        "counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back up only the PKAS rows changed by the Codex user-task-only migration."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    arguments = parser.parse_args()
    print(json.dumps(backup(arguments.database, arguments.output_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
