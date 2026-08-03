import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPDATA_ROOT = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="PKAS_",
        extra="ignore",
    )

    app_name: str = "个人知识与智能协作系统"
    app_version: str = "0.1.0"
    project_root: Path = PROJECT_ROOT
    data_root: Path = PROJECT_ROOT / "data"
    host: str = "127.0.0.1"
    port: int = 8765
    max_source_bytes: int = 256 * 1024 * 1024
    max_import_files: int = 5000
    weflow_export_records_path: Path = (
        APPDATA_ROOT / "weflow" / "weflow-export-records.json"
    )
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
    def vault_root(self) -> Path:
        return self.data_root / "raw" / "sha256"

    @property
    def web_dist(self) -> Path:
        return self.project_root / "web" / "dist"

    def ensure_directories(self) -> None:
        for path in (
            self.data_root / "raw",
            self.data_root / "normalized",
            self.data_root / "knowledge",
            self.data_root / "self",
            self.data_root / "distill",
            self.data_root / "index",
            self.data_root / "runs",
            self.vault_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
