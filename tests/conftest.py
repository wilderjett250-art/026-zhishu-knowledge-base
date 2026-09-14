from pathlib import Path

import pytest

from pkas.config import Settings
from pkas.system import KnowledgeSystem


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    project_root = tmp_path / "knowledge-system"
    project_root.mkdir()
    settings = Settings(
        project_root=project_root,
        data_root=project_root / "data",
        integration_home=tmp_path / "integration-home",
        allowed_origins=[],
        deepseek_api_key=None,
        document_docling_enabled=False,
        document_paddleocr_enabled=False,
        document_paddleocr_base_url=None,
        document_paddleocr_api_key=None,
        document_allow_remote_processing=False,
        document_allow_restricted_remote_processing=False,
        thread_journal_enabled=False,
    )
    settings.ensure_directories()
    return settings


@pytest.fixture
def knowledge_system(test_settings: Settings) -> KnowledgeSystem:
    return KnowledgeSystem.create(test_settings)


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    path = tmp_path / "authorized-source"
    path.mkdir()
    return path
