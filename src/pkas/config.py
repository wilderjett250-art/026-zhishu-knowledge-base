import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(os.environ.get("PKAS_PROJECT_ROOT", Path(__file__).resolve().parents[2]))
ENV_FILE = Path(os.environ.get("PKAS_ENV_FILE", PROJECT_ROOT / ".env"))
APPDATA_ROOT = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="PKAS_",
        extra="ignore",
    )

    app_name: str = "个人知识与智能协作系统"
    app_version: str = "0.1.19"
    project_root: Path = PROJECT_ROOT
    data_root: Path = PROJECT_ROOT / "data"
    integration_home: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    max_source_bytes: int = 256 * 1024 * 1024
    max_import_files: int = 5000
    document_ai_enhancement_enabled: bool = False
    document_docling_enabled: bool = False
    document_paddleocr_enabled: bool = False
    document_paddleocr_base_url: str | None = None
    document_paddleocr_api_key: SecretStr | None = None
    document_allow_remote_processing: bool = False
    document_allow_restricted_remote_processing: bool = False
    document_parser_timeout_seconds: int = 240
    document_visual_min_page_chars: int = 40
    document_visual_max_pages: int = 50
    document_visual_render_dpi: int = 180
    document_block_metadata_limit: int = 10_000
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_flash_model: str = "deepseek-v4-flash"
    deepseek_pro_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: int = 90
    embedding_enabled: bool = True
    embedding_api_key: SecretStr | None = None
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    # Full text is the default knowledge lane.  Only explicitly selected L3
    # material enters the vector lane, keeping remote embedding work bounded.
    embedding_scope: Literal["all_formal", "selected_l3"] = "selected_l3"
    embedding_timeout_seconds: int = 60
    embedding_batch_size: int = 32
    embedding_max_attempts: int = 6
    embedding_retry_base_seconds: float = 5.0
    embedding_retry_max_seconds: float = 60.0
    embedding_allow_restricted_remote_processing: bool = False
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_timeout_seconds: int = 30
    rerank_candidate_limit: int = 20
    rerank_complex_candidate_limit: int = 40
    rerank_document_char_limit: int = 3_000
    rerank_total_char_budget: int = 60_000
    fusion_fts_weight: float = 1.0
    fusion_vector_weight: float = 1.0
    # The bundled Qdrant runs as a local HTTP sidecar so the EXE, dashboard and
    # Codex MCP can share one collection.  Embedded mode remains available for
    # isolated tests, but it is unsafe as the production default because the
    # local storage folder has a single-process lock.
    qdrant_mode: Literal["embedded", "service"] = "service"
    qdrant_path: Path | None = None
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "pkas_chunks_v1"
    # Codex itself is the interactive agent.  PKAS keeps its former
    # LangGraph/DeepSeek orchestration disabled so the product stays focused
    # on cataloguing, indexing and evidence-backed retrieval.
    agent_runtime_enabled: bool = False
    agent_daily_input_token_budget: int = 200_000
    agent_daily_output_token_budget: int = 20_000
    agent_min_output_tokens_per_call: int = 128
    agent_max_context_chars: int = 24_000
    agent_max_tool_rounds: int = 4
    # Codex conversation data is an archive lane, not ordinary knowledge.  It is
    # deliberately opt-in so a fresh installation never turns every task into
    # searchable context or background model work.
    codex_task_capture_enabled: bool = False
    codex_history_sync_enabled: bool = False
    agent_daily_closeout_enabled: bool = False
    agent_daily_timezone: str = "local"
    agent_daily_max_jobs: int = 1
    agent_daily_max_sources_per_thread: int = 3
    agent_daily_max_sources_total: int = 30
    thread_journal_enabled: bool = False
    thread_journal_interval_seconds: int = 7 * 24 * 60 * 60
    thread_journal_backlog_interval_seconds: int = 60
    thread_journal_batch_files: int = 8
    thread_journal_chars_per_session: int = 8_000
    weflow_export_records_path: Path = APPDATA_ROOT / "weflow" / "weflow-export-records.json"
    weflow_manual_timeout_seconds: int = 1800
    allowed_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ]

    @property
    def database_path(self) -> Path:
        return self.data_root / "index" / "pkas.sqlite"

    @property
    def agent_checkpoint_path(self) -> Path:
        return self.data_root / "index" / "langgraph-checkpoints.sqlite"

    @property
    def vault_root(self) -> Path:
        return self.data_root / "raw" / "sha256"

    @property
    def web_dist(self) -> Path:
        return self.project_root / "web" / "dist"

    @property
    def qdrant_storage_path(self) -> Path:
        return self.qdrant_path or (self.data_root / "vector" / "qdrant")

    @property
    def client_home(self) -> Path:
        """Explicit test/replica override, otherwise the current OS user home."""
        return self.integration_home or Path.home()

    @property
    def deepseek_enabled(self) -> bool:
        return bool(self.deepseek_api_key and self.deepseek_api_key.get_secret_value().strip())

    @property
    def paddleocr_enabled(self) -> bool:
        return bool(
            self.document_paddleocr_enabled
            and self.document_paddleocr_base_url
            and self.document_allow_remote_processing
        )

    @property
    def cloud_embedding_enabled(self) -> bool:
        return bool(self.embedding_enabled and self.embedding_api_key)

    def ensure_directories(self) -> None:
        for path in (
            self.data_root / "raw",
            self.data_root / "normalized",
            self.data_root / "knowledge",
            self.data_root / "self",
            self.data_root / "distill",
            self.data_root / "index",
            self.data_root / "runs",
            self.data_root / "reports",
            self.vault_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
