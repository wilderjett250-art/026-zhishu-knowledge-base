import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pkas.backup_manager import BACKUP_PROTOCOL, BackupError, BackupManager
from pkas.config import Settings
from pkas.system import KnowledgeSystem


def _build_source(system: KnowledgeSystem, source_root: Path) -> Path:
    source = source_root / "恢复验证.md"
    source.write_text("备份恢复必须保留原文、全文索引和来源定位。", encoding="utf-8")
    result = system.ingestion.import_file(
        source,
        domain="work",
        privacy="private",
    )
    return Path(result["vault_path"])


def test_verified_bundle_restores_to_isolated_data_root(
    knowledge_system: KnowledgeSystem,
    test_settings: Settings,
    source_root: Path,
    tmp_path: Path,
) -> None:
    original_vault = _build_source(knowledge_system, source_root)
    with closing(sqlite3.connect(test_settings.agent_checkpoint_path)) as connection:
        connection.execute("CREATE TABLE checkpoints (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO checkpoints VALUES ('checkpoint-1')")
        connection.commit()

    manager = BackupManager(test_settings)
    backup = manager.create_bundle(label="test", retention=3)
    bundle = Path(backup["bundle"])

    assert backup["protocol"] == BACKUP_PROTOCOL
    assert backup["secrets_included"] is False
    assert manager.verify_bundle(bundle)["status"] == "verified"

    target = tmp_path / "restored-data"
    restored = manager.restore_bundle(bundle, target)

    assert restored["status"] == "restored"
    assert restored["missing_vault_files"] == 0
    assert restored["chunks"] == restored["fts_rows"] == 1
    assert restored["sync_roots_disabled"] is True
    assert restored["secrets_restored"] is False
    assert (target / "index" / "langgraph-checkpoints.sqlite").is_file()
    with closing(sqlite3.connect(target / "index" / "pkas.sqlite")) as connection:
        restored_vault = Path(
            connection.execute("SELECT vault_path FROM sources").fetchone()[0]
        )
        enabled_roots = int(
            connection.execute(
                "SELECT COUNT(1) FROM sync_roots WHERE enabled=1"
            ).fetchone()[0]
        )
    assert restored_vault != original_vault
    assert restored_vault.is_file()
    assert target in restored_vault.parents
    assert enabled_roots == 0
    report = json.loads(
        (target / "runs" / "restore-verification.json").read_text(encoding="utf-8")
    )
    assert report["verified_backup"] is True


def test_backup_retention_only_prunes_matching_managed_family(
    knowledge_system: KnowledgeSystem,
    test_settings: Settings,
    source_root: Path,
    tmp_path: Path,
) -> None:
    _build_source(knowledge_system, source_root)
    output = tmp_path / "bundles"
    preserved = output / "phase-snapshot-do-not-touch"
    preserved.mkdir(parents=True)
    (preserved / "manifest.json").write_text("{}", encoding="utf-8")
    manager = BackupManager(test_settings)

    for _ in range(4):
        manager.create_bundle(label="retention", retention=3, output_root=output)

    assert len(list(output.glob("pkas-retention-*"))) == 3
    assert preserved.is_dir()


def test_restore_refuses_active_or_nonempty_target(
    knowledge_system: KnowledgeSystem,
    test_settings: Settings,
    source_root: Path,
    tmp_path: Path,
) -> None:
    _build_source(knowledge_system, source_root)
    manager = BackupManager(test_settings)
    bundle = Path(manager.create_bundle(label="guard")["bundle"])

    with pytest.raises(BackupError, match="active"):
        manager.restore_bundle(bundle, test_settings.data_root)
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(BackupError, match="absent or empty"):
        manager.restore_bundle(bundle, nonempty)
