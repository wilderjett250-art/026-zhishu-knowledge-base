import json
import sqlite3
import stat
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


@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_restore_keeps_external_source_references_and_reports_missing_originals(
    knowledge_system: KnowledgeSystem,
    test_settings: Settings,
    source_root: Path,
    tmp_path: Path,
    legacy_manifest: bool,
) -> None:
    vaulted_path = _build_source(knowledge_system, source_root)
    linked_path = source_root / "linked.md"
    linked_path.write_text("链接原件由用户保管，不属于知识库原文仓库。", encoding="utf-8")
    knowledge_system.ingestion.import_file(linked_path, domain="work", privacy="private")
    missing_path = source_root / "missing-original.md"
    with closing(sqlite3.connect(test_settings.database_path)) as connection:
        connection.execute(
            "UPDATE sources SET vault_path=? WHERE original_uri=?",
            (str(missing_path), str(linked_path)),
        )
        connection.commit()

    manager = BackupManager(test_settings)
    bundle = Path(manager.create_bundle(label="external", retention=1)["bundle"])
    if legacy_manifest:
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("source_raw_root")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restored_root = tmp_path / "external-restored"
    report = manager.restore_bundle(bundle, restored_root)

    assert report["status"] == "restored"
    assert report["missing_vault_files"] == 0
    assert report["vault_paths_remapped"] == 1
    assert report["external_source_refs"] == 1
    assert report["unavailable_external_source_refs"] == 1
    with closing(sqlite3.connect(restored_root / "index" / "pkas.sqlite")) as connection:
        paths = {
            original_uri: vault_path
            for original_uri, vault_path in connection.execute(
                "SELECT original_uri, vault_path FROM sources"
            )
        }
    assert paths[str(linked_path)] == str(missing_path)
    assert Path(paths[str(source_root / "恢复验证.md")]).is_file()
    assert Path(paths[str(source_root / "恢复验证.md")]) != vaulted_path


def test_restore_reports_missing_managed_raw_without_masking_readonly_cleanup(
    knowledge_system: KnowledgeSystem,
    test_settings: Settings,
    source_root: Path,
    tmp_path: Path,
) -> None:
    vaulted_path = _build_source(knowledge_system, source_root)
    vaulted_path.chmod(stat.S_IREAD)
    missing_path = test_settings.vault_root / "ff" / "not-backed-up.md"
    with closing(sqlite3.connect(test_settings.database_path)) as connection:
        connection.execute(
            "UPDATE sources SET vault_path=?",
            (str(missing_path),),
        )
        connection.commit()

    manager = BackupManager(test_settings)
    bundle = Path(manager.create_bundle(label="missing-raw", retention=1)["bundle"])
    target = tmp_path / "missing-raw-restored"
    with pytest.raises(BackupError, match="missing_vault_files=1"):
        manager.restore_bundle(bundle, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".partial-restore-*"))


def test_cleanup_failure_keeps_the_original_restore_error(
    knowledge_system: KnowledgeSystem,
    test_settings: Settings,
    source_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_source(knowledge_system, source_root)
    missing_path = test_settings.vault_root / "ff" / "not-backed-up.md"
    with closing(sqlite3.connect(test_settings.database_path)) as connection:
        connection.execute("UPDATE sources SET vault_path=?", (str(missing_path),))
        connection.commit()
    manager = BackupManager(test_settings)
    bundle = Path(manager.create_bundle(label="cleanup-error", retention=1)["bundle"])

    def blocked_cleanup(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("simulated lock")

    with monkeypatch.context() as patch:
        patch.setattr("pkas.backup_manager.shutil.rmtree", blocked_cleanup)
        with pytest.raises(BackupError, match="missing_vault_files=1") as error:
            manager.restore_bundle(bundle, tmp_path / "cleanup-error-restored")
    assert any("Cleanup of the isolated restore staging" in note for note in error.value.__notes__)


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
