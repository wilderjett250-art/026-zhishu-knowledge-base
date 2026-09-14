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

CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    parent_id TEXT REFERENCES blocks(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    text_content TEXT NOT NULL,
    locator TEXT NOT NULL,
    page INTEGER,
    bbox_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    char_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, sequence)
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
    block_id TEXT,
    chunk_kind TEXT NOT NULL DEFAULT 'legacy',
    chunker_version TEXT NOT NULL DEFAULT 'legacy-v1',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk_blocks (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chunk_id, block_id)
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

CREATE TABLE IF NOT EXISTS agent_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    phase TEXT NOT NULL,
    action_name TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    error_json TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS agent_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
    workspace_path TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(job_type, source_id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
    task_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    thinking_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    application_cache_hit INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    response_json TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vector_index_state (
    chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    point_id TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    payload_version INTEGER NOT NULL DEFAULT 1,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_eval_cases (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    expected_source_ids_json TEXT NOT NULL DEFAULT '[]',
    domain TEXT,
    include_restricted INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    match_policy TEXT NOT NULL DEFAULT 'any',
    category TEXT NOT NULL DEFAULT 'general',
    difficulty TEXT NOT NULL DEFAULT 'normal',
    review_status TEXT NOT NULL DEFAULT 'reviewed',
    judgment_scope_source_ids_json TEXT NOT NULL DEFAULT '[]',
    reviewed_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_eval_judgments (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES rag_eval_cases(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    relevance_grade INTEGER NOT NULL CHECK(relevance_grade BETWEEN 0 AND 3),
    judgment_basis TEXT NOT NULL DEFAULT 'human',
    reviewer TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, source_id)
);

CREATE TABLE IF NOT EXISTS rag_eval_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    top_k INTEGER NOT NULL,
    case_count INTEGER NOT NULL,
    hit_rate REAL NOT NULL,
    recall_at_k REAL NOT NULL,
    precision_at_k REAL NOT NULL,
    mrr REAL NOT NULL,
    ndcg_at_k REAL NOT NULL DEFAULT 0,
    judgment_coverage REAL NOT NULL DEFAULT 0,
    strata_json TEXT NOT NULL DEFAULT '{}',
    eval_protocol TEXT NOT NULL DEFAULT 'legacy-v1',
    retrieval_modes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_eval_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES rag_eval_runs(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES rag_eval_cases(id) ON DELETE CASCADE,
    hit INTEGER NOT NULL,
    recall_at_k REAL NOT NULL,
    precision_at_k REAL NOT NULL,
    reciprocal_rank REAL NOT NULL,
    ndcg_at_k REAL NOT NULL DEFAULT 0,
    judgment_coverage REAL NOT NULL DEFAULT 0,
    returned_source_ids_json TEXT NOT NULL DEFAULT '[]',
    warning_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS connectors (
    id TEXT PRIMARY KEY,
    connector_type TEXT NOT NULL,
    name TEXT NOT NULL,
    base_url TEXT,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    last_health_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(connector_type, base_url)
);

CREATE TABLE IF NOT EXISTS connector_snapshots (
    id TEXT PRIMARY KEY,
    connector_id TEXT REFERENCES connectors(id) ON DELETE SET NULL,
    conversation_id TEXT,
    content_hash TEXT NOT NULL UNIQUE,
    vault_path TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    source_uri TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    customer_type TEXT NOT NULL,
    remark TEXT,
    nickname TEXT,
    alias TEXT,
    company TEXT,
    stage TEXT NOT NULL DEFAULT 'active',
    tags_json TEXT NOT NULL DEFAULT '[]',
    privacy TEXT NOT NULL DEFAULT 'restricted',
    summary TEXT,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    last_message_at INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, platform_id)
);

CREATE TABLE IF NOT EXISTS customer_conversations (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    connector_id TEXT REFERENCES connectors(id) ON DELETE SET NULL,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    name TEXT NOT NULL,
    conversation_type TEXT NOT NULL,
    owner_platform_id TEXT,
    privacy TEXT NOT NULL DEFAULT 'restricted',
    message_count INTEGER NOT NULL DEFAULT 0,
    first_message_at INTEGER,
    last_message_at INTEGER,
    sync_since INTEGER NOT NULL DEFAULT 0,
    sync_offset INTEGER NOT NULL DEFAULT 0,
    sync_watermark INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, platform_id)
);

CREATE TABLE IF NOT EXISTS customer_members (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES customer_conversations(id) ON DELETE CASCADE,
    platform_id TEXT NOT NULL,
    account_name TEXT,
    group_nickname TEXT,
    avatar_url TEXT,
    role TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(conversation_id, platform_id)
);

CREATE TABLE IF NOT EXISTS customer_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES customer_conversations(id) ON DELETE CASCADE,
    snapshot_id TEXT REFERENCES connector_snapshots(id) ON DELETE SET NULL,
    platform_message_id TEXT,
    local_id TEXT,
    sender_platform_id TEXT,
    sender_name TEXT,
    is_self INTEGER NOT NULL DEFAULT 0,
    sent_at INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    content TEXT NOT NULL,
    raw_content TEXT,
    parsed_content TEXT,
    reply_to_message_id TEXT,
    quote_json TEXT,
    media_json TEXT,
    source_hash TEXT NOT NULL,
    privacy TEXT NOT NULL DEFAULT 'restricted',
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, source_hash)
);

CREATE TABLE IF NOT EXISTS customer_signals (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    conversation_id TEXT REFERENCES customer_conversations(id) ON DELETE SET NULL,
    signal_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    due_at TEXT,
    evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'low',
    approval_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personal_activity_items (
    id TEXT PRIMARY KEY,
    local_date TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    activity_status TEXT NOT NULL,
    certainty TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
    conversation_ids_json TEXT NOT NULL DEFAULT '[]',
    extraction_method TEXT NOT NULL,
    model TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(local_date, statement)
);

CREATE TABLE IF NOT EXISTS personal_daily_summaries (
    local_date TEXT PRIMARY KEY,
    factual_summary TEXT NOT NULL,
    inferred_focus_json TEXT NOT NULL DEFAULT '[]',
    open_items_json TEXT NOT NULL DEFAULT '[]',
    source_message_count INTEGER NOT NULL DEFAULT 0,
    candidate_message_count INTEGER NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    extraction_method TEXT NOT NULL,
    model TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_roots (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_uri TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    domain TEXT NOT NULL,
    privacy TEXT NOT NULL,
    sync_mode TEXT NOT NULL,
    recursive INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    last_scan_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(connector_type, root_uri)
);

CREATE TABLE IF NOT EXISTS sync_items (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL REFERENCES sync_roots(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    relative_path TEXT,
    byte_size INTEGER NOT NULL DEFAULT 0,
    modified_ns INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    indexed_at TEXT,
    UNIQUE(root_id, external_id)
);

CREATE TABLE IF NOT EXISTS codex_session_cursors (
    root_id TEXT NOT NULL REFERENCES sync_roots(id) ON DELETE CASCADE,
    source_uri TEXT NOT NULL,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    modified_ns INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_scan_at TEXT NOT NULL,
    PRIMARY KEY(root_id, source_uri)
);

CREATE TABLE IF NOT EXISTS sync_root_stats (
    root_id TEXT PRIMARY KEY REFERENCES sync_roots(id) ON DELETE CASCADE,
    item_count INTEGER NOT NULL DEFAULT 0,
    active_count INTEGER NOT NULL DEFAULT 0,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_result_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    client_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    knowledge_domains_json TEXT NOT NULL DEFAULT '[]',
    allowed_privacy_json TEXT NOT NULL DEFAULT '["public", "private"]',
    daily_input_token_budget INTEGER NOT NULL DEFAULT 0,
    daily_output_token_budget INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_profile_bindings (
    profile_id TEXT NOT NULL REFERENCES capability_profiles(id) ON DELETE CASCADE,
    asset_kind TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY(profile_id, asset_kind, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_sources_domain ON sources(domain);
CREATE INDEX IF NOT EXISTS idx_sources_privacy ON sources(privacy);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_blocks_document ON blocks(document_id, sequence);
CREATE INDEX IF NOT EXISTS idx_blocks_parent ON blocks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunk_blocks_block ON chunk_blocks(block_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_document ON messages(document_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_created ON workflow_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status ON agent_jobs(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(agent_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_vector_state_model
    ON vector_index_state(provider, model, collection_name);
CREATE INDEX IF NOT EXISTS idx_rag_eval_results_run ON rag_eval_results(run_id);
CREATE INDEX IF NOT EXISTS idx_rag_eval_runs_created ON rag_eval_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_eval_judgments_case ON rag_eval_judgments(case_id);
CREATE INDEX IF NOT EXISTS idx_index_outbox_ready
ON index_outbox(status, available_at, id);

CREATE TRIGGER IF NOT EXISTS trg_chunks_vector_insert
AFTER INSERT ON chunks
WHEN (SELECT source_type FROM sources WHERE id=NEW.source_id) <> 'codex-turn'
BEGIN
    INSERT INTO index_outbox(
        event_key, operation, entity_type, entity_id, status,
        attempts, available_at, created_at, updated_at
    ) VALUES (
        'vector:upsert:' || NEW.id, 'upsert', 'chunk', NEW.id, 'pending', 0,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ) ON CONFLICT(event_key) DO UPDATE SET
        status='pending', attempts=0, last_error_code=NULL,
        available_at=excluded.available_at, updated_at=excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS trg_chunks_vector_update
AFTER UPDATE OF title, text_content, domain, privacy ON chunks
WHEN (SELECT source_type FROM sources WHERE id=NEW.source_id) <> 'codex-turn'
BEGIN
    INSERT INTO index_outbox(
        event_key, operation, entity_type, entity_id, status,
        attempts, available_at, created_at, updated_at
    ) VALUES (
        'vector:upsert:' || NEW.id, 'upsert', 'chunk', NEW.id, 'pending', 0,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ) ON CONFLICT(event_key) DO UPDATE SET
        status='pending', attempts=0, last_error_code=NULL,
        available_at=excluded.available_at, updated_at=excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS trg_chunks_vector_delete
AFTER DELETE ON chunks
WHEN COALESCE(
    (SELECT source_type FROM sources WHERE id=OLD.source_id), ''
) <> 'codex-turn'
BEGIN
    INSERT INTO index_outbox(
        event_key, operation, entity_type, entity_id, status,
        attempts, available_at, created_at, updated_at
    ) VALUES (
        'vector:delete:' || OLD.id, 'delete', 'chunk', OLD.id, 'pending', 0,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ) ON CONFLICT(event_key) DO UPDATE SET
        status='pending', attempts=0, last_error_code=NULL,
        available_at=excluded.available_at, updated_at=excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS trg_sources_vector_reconcile
AFTER UPDATE OF status, domain, privacy ON sources BEGIN
    INSERT INTO index_outbox(
        event_key, operation, entity_type, entity_id, status,
        attempts, available_at, created_at, updated_at
    ) VALUES (
        'vector:reconcile-source:' || NEW.id, 'reconcile', 'source', NEW.id,
        'pending', 0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ) ON CONFLICT(event_key) DO UPDATE SET
        status='pending', attempts=0, last_error_code=NULL,
        available_at=excluded.available_at, updated_at=excluded.updated_at;
END;
CREATE INDEX IF NOT EXISTS idx_persona_status ON persona_observations(approval_status);
CREATE INDEX IF NOT EXISTS idx_connectors_type ON connectors(connector_type);
CREATE INDEX IF NOT EXISTS idx_snapshots_conversation ON connector_snapshots(conversation_id);
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(display_name);
CREATE INDEX IF NOT EXISTS idx_customer_conversations_customer
    ON customer_conversations(customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_messages_conversation_time
    ON customer_messages(conversation_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_messages_sender ON customer_messages(sender_platform_id);
CREATE INDEX IF NOT EXISTS idx_customer_signals_customer ON customer_signals(customer_id);
CREATE INDEX IF NOT EXISTS idx_customer_signals_status ON customer_signals(approval_status, status);
CREATE INDEX IF NOT EXISTS idx_personal_activity_date
    ON personal_activity_items(local_date DESC, certainty, activity_status);
CREATE INDEX IF NOT EXISTS idx_personal_activity_review
    ON personal_activity_items(review_status, local_date DESC);
CREATE INDEX IF NOT EXISTS idx_sync_roots_type ON sync_roots(connector_type, enabled);
CREATE INDEX IF NOT EXISTS idx_sync_items_root_state ON sync_items(root_id, state);
CREATE INDEX IF NOT EXISTS idx_sync_items_source ON sync_items(source_id);
CREATE INDEX IF NOT EXISTS idx_sync_items_relative_path ON sync_items(relative_path);
CREATE INDEX IF NOT EXISTS idx_capability_profiles_client
    ON capability_profiles(client_id, status);
CREATE INDEX IF NOT EXISTS idx_capability_profile_bindings_kind
    ON capability_profile_bindings(asset_kind, asset_id);
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
            existing_schema_version = 0
            try:
                row = connection.execute(
                    "SELECT value FROM app_meta WHERE key='schema_version'"
                ).fetchone()
                existing_schema_version = int(row["value"]) if row else 0
            except sqlite3.OperationalError:
                existing_schema_version = 0
            if existing_schema_version < 15:
                connection.executescript(
                    """DROP TRIGGER IF EXISTS trg_chunks_vector_insert;
                    DROP TRIGGER IF EXISTS trg_chunks_vector_update;
                    DROP TRIGGER IF EXISTS trg_chunks_vector_delete;
                    DROP TRIGGER IF EXISTS trg_sources_vector_reconcile;"""
                )
            connection.executescript(BASE_SCHEMA)
            self._ensure_column(
                connection,
                "vector_index_state",
                "payload_version",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                connection,
                "rag_eval_cases",
                "match_policy",
                "TEXT NOT NULL DEFAULT 'any'",
            )
            self._ensure_column(
                connection, "rag_eval_cases", "category", "TEXT NOT NULL DEFAULT 'general'"
            )
            self._ensure_column(
                connection, "rag_eval_cases", "difficulty", "TEXT NOT NULL DEFAULT 'normal'"
            )
            self._ensure_column(
                connection,
                "rag_eval_cases",
                "review_status",
                "TEXT NOT NULL DEFAULT 'reviewed'",
            )
            self._ensure_column(
                connection,
                "rag_eval_cases",
                "judgment_scope_source_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(connection, "rag_eval_cases", "reviewed_at", "TEXT")
            self._ensure_column(connection, "rag_eval_runs", "ndcg_at_k", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(
                connection,
                "rag_eval_runs",
                "judgment_coverage",
                "REAL NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection, "rag_eval_runs", "strata_json", "TEXT NOT NULL DEFAULT '{}'"
            )
            self._ensure_column(
                connection,
                "rag_eval_runs",
                "eval_protocol",
                "TEXT NOT NULL DEFAULT 'legacy-v1'",
            )
            self._ensure_column(
                connection, "rag_eval_results", "ndcg_at_k", "REAL NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                connection,
                "rag_eval_results",
                "judgment_coverage",
                "REAL NOT NULL DEFAULT 0",
            )
            connection.execute(
                """INSERT OR IGNORE INTO rag_eval_judgments(
                id, case_id, source_id, relevance_grade, judgment_basis,
                reviewer, created_at, updated_at
                )
                SELECT 'evaljudgment_' || lower(hex(randomblob(16))), c.id,
                       CAST(j.value AS TEXT), 3, 'expected-source', 'migration',
                       c.created_at, c.updated_at
                FROM rag_eval_cases c, json_each(c.expected_source_ids_json) j
                JOIN sources s ON s.id=CAST(j.value AS TEXT)"""
            )
            self._ensure_column(connection, "chunks", "block_id", "TEXT")
            self._ensure_column(
                connection,
                "chunks",
                "chunk_kind",
                "TEXT NOT NULL DEFAULT 'legacy'",
            )
            self._ensure_column(
                connection,
                "chunks",
                "chunker_version",
                "TEXT NOT NULL DEFAULT 'legacy-v1'",
            )
            tokenizer = self._ensure_fts(connection)
            customer_tokenizer = self._ensure_customer_fts(connection)
            connection.execute(
                """
                UPDATE sources
                SET status = 'superseded'
                WHERE source_type = 'codex-turn'
                  AND status = 'indexed'
                  AND instr(lower(original_name), 'codex ambient suggestions') > 0
                  AND instr(
                      lower(COALESCE(json_extract(metadata_json, '$.cwd'), '')),
                      '\\windowsapps\\openai.codex_'
                  ) > 0
                """
            )
            connection.execute(
                """
                UPDATE sources
                SET status = 'superseded'
                WHERE source_type = 'codex-turn'
                  AND status = 'indexed'
                  AND instr(
                      lower(original_name),
                      'you are a helpful assistant. you will be presented with a user prompt'
                  ) > 0
                  AND instr(lower(original_name), 'short title') > 0
                """
            )
            connection.execute(
                """
                UPDATE sources
                SET status = 'superseded'
                WHERE source_type = 'codex-turn'
                  AND status = 'indexed'
                  AND id IN (
                      SELECT s.id
                      FROM sources s
                      JOIN documents d ON d.source_id = s.id
                      WHERE instr(
                          lower(d.text_content),
                          'you write the one-line activity update displayed beneath '
                          || 'an existing codex task title'
                      ) > 0
                  )
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES('schema_version', '19')"
            )
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES('fts_tokenizer', ?)",
                (tokenizer,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO app_meta(key, value) VALUES('customer_fts_tokenizer', ?)",
                (customer_tokenizer,),
            )
            connection.commit()

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

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

    @staticmethod
    def _ensure_customer_fts(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='customer_messages_fts'"
        ).fetchone()
        if row:
            tokenizer = connection.execute(
                "SELECT value FROM app_meta WHERE key='customer_fts_tokenizer'"
            ).fetchone()
            return tokenizer["value"] if tokenizer else "existing"

        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE customer_messages_fts USING fts5(
                    message_id UNINDEXED,
                    customer_id UNINDEXED,
                    conversation_id UNINDEXED,
                    sender_name,
                    content,
                    privacy UNINDEXED,
                    tokenize='trigram'
                )
                """
            )
            return "trigram"
        except sqlite3.OperationalError:
            connection.execute(
                """
                CREATE VIRTUAL TABLE customer_messages_fts USING fts5(
                    message_id UNINDEXED,
                    customer_id UNINDEXED,
                    conversation_id UNINDEXED,
                    sender_name,
                    content,
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
