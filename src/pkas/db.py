import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pkas.config import Settings, get_settings

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    original_uri TEXT NOT NULL,
    original_name TEXT NOT NULL,
    vault_path TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL,
    mime_type TEXT,
    domain TEXT NOT NULL,
    privacy TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT,
    ingested_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    text_content TEXT NOT NULL,
    language TEXT,
    event_time TEXT,
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    conversation_id TEXT,
    sequence INTEGER NOT NULL,
    speaker TEXT,
    sent_at TEXT,
    text_content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    title TEXT NOT NULL,
    text_content TEXT NOT NULL,
    locator TEXT NOT NULL,
    domain TEXT NOT NULL,
    privacy TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    knowledge_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence TEXT NOT NULL,
    review_status TEXT NOT NULL,
    privacy TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    supersedes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    locator TEXT,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_observations (
    id TEXT PRIMARY KEY,
    observation_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL,
    counterexamples_json TEXT NOT NULL DEFAULT '[]',
    approval_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS distillation_examples (
    id TEXT PRIMARY KEY,
    example_type TEXT NOT NULL,
    input_text TEXT NOT NULL,
    preferred_output TEXT,
    rejected_output TEXT,
    rationale TEXT,
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    privacy TEXT NOT NULL,
    quality_score REAL,
    approval_status TEXT NOT NULL,
    dataset_split TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    error_json TEXT,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    selected_domain TEXT,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS approval_records (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_domain ON sources(domain);
CREATE INDEX IF NOT EXISTS idx_sources_privacy ON sources(privacy);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_messages_document ON messages(document_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_created ON workflow_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_persona_status ON persona_observations(approval_status);
"""


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = Path(self.settings.database_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.settings.ensure_directories()
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.settings.ensure_directories()
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(BASE_SCHEMA)
            tokenizer = self._ensure_fts(connection)
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES('schema_version', '1')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES('fts_tokenizer', ?)",
                (tokenizer,),
            )
            connection.commit()

    @staticmethod
    def _ensure_fts(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
        ).fetchone()
        if row:
            tokenizer = connection.execute(
                "SELECT value FROM app_meta WHERE key='fts_tokenizer'"
            ).fetchone()
            return tokenizer["value"] if tokenizer else "existing"

        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    title,
                    content,
                    domain UNINDEXED,
                    privacy UNINDEXED,
                    tokenize='trigram'
                )
                """
            )
            return "trigram"
        except sqlite3.OperationalError:
            connection.execute(
                """
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    title,
                    content,
                    domain UNINDEXED,
                    privacy UNINDEXED,
                    tokenize='unicode61'
                )
                """
            )
            return "unicode61"

    def health(self) -> dict[str, str]:
        self.initialize()
        with self.connect() as connection:
            sqlite_version = connection.execute("SELECT sqlite_version() AS version").fetchone()
            tokenizer = connection.execute(
                "SELECT value FROM app_meta WHERE key='fts_tokenizer'"
            ).fetchone()
        return {
            "database": str(self.path),
            "sqlite_version": sqlite_version["version"],
            "fts_tokenizer": tokenizer["value"] if tokenizer else "unknown",
        }
