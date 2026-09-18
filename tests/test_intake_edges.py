from collections import namedtuple
from pathlib import Path

import pytest
from test_intake import execute, preview

from pkas.intake import IntakeRequest, IntakeService


def test_backup_capacity_counts_one_new_snapshot_plus_batch_reserve():
    database_bytes = 5 * 1024**3
    pending_source_bytes = 6 * 1024**2

    required = IntakeService._backup_required_free_bytes(
        database_bytes=database_bytes,
        pending_source_bytes=pending_source_bytes,
    )

    assert required == database_bytes + 512 * 1024**2 + 64 * 1024**2


def test_backup_refuses_only_when_one_new_snapshot_and_reserve_do_not_fit(
    knowledge_system, source_root, monkeypatch
):
    (source_root / "a.md").write_text("capacity fixture")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    source_bytes = knowledge_system.settings.database_path.stat().st_size
    required = service._backup_required_free_bytes(
        database_bytes=source_bytes,
        pending_source_bytes=len(b"capacity fixture"),
    )
    usage_type = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        "pkas.intake.shutil.disk_usage",
        lambda _path: usage_type(required - 1, 0, required - 1),
    )

    with pytest.raises(ValueError, match="空间不足"):
        service._backup(plan)


def test_preview_exposes_recovery_capacity_and_blocks_confirmation_early(
    knowledge_system, source_root, monkeypatch
):
    (source_root / "a.md").write_text("capacity preview fixture")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    source_bytes = knowledge_system.settings.database_path.stat().st_size
    required = service._backup_required_free_bytes(
        database_bytes=source_bytes,
        pending_source_bytes=len(b"capacity preview fixture"),
    )
    usage_type = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        "pkas.intake.shutil.disk_usage",
        lambda _path: usage_type(required - 1, 0, required - 1),
    )

    view = service.view(plan["id"])

    assert not view["recovery_capacity"]["ready"]
    assert view["recovery_capacity"]["shortfall_bytes"] == 1
    with pytest.raises(ValueError, match="空间不足"):
        service.run(plan["id"], True)
    assert service.read(plan["id"])["state"] == "ready"


def test_updated_reference_supersedes(knowledge_system, source_root):
    note = source_root / "a.md"
    note.write_text("first version")
    service = IntakeService(knowledge_system)
    first = execute(service, preview(service, source_root))["items"][0]["source_id"]
    note.write_text("second longer version")
    second = execute(service, preview(service, source_root))["items"][0]["source_id"]
    assert first != second
    with knowledge_system.database.connect() as c:
        assert (
            c.execute("select status from sources where id=?", (first,)).fetchone()[0]
            == "superseded"
        )


def test_link_is_not_traversed(knowledge_system, source_root, tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (other / "a.md").write_text("outside")
    try:
        (source_root / "link").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    assert plan["items"] == []


def test_recovery_keeps_prechange_snapshot(knowledge_system, source_root):
    (source_root / "a.md").write_text("recovery fixture")
    service = IntakeService(knowledge_system)
    result = execute(service, preview(service, source_root))
    import sqlite3

    with sqlite3.connect(result["recovery"]) as c:
        assert c.execute("pragma quick_check").fetchone()[0] == "ok"
        assert c.execute("select count(*) from sources").fetchone()[0] == 0


def test_custom_recovery_root_is_previewed_then_used_only_on_confirmation(
    knowledge_system, source_root, tmp_path
):
    (source_root / "a.md").write_text("external recovery fixture")
    recovery_root = tmp_path / "separate-recovery-disk"
    service = IntakeService(knowledge_system)

    plan = preview(service, source_root, recovery_root=str(recovery_root))

    assert plan["recovery_root"] == str(recovery_root.resolve())
    # Capacity is a read-only view concern, so inspect it through the same
    # endpoint shape consumed by the desktop UI after scanning finishes.
    viewed = service.view(plan["id"])
    assert viewed["recovery_capacity"]["recovery_root"] == str(recovery_root.resolve())
    assert not recovery_root.exists()

    result = execute(service, plan)
    recovery = Path(result["recovery"])
    assert recovery.is_relative_to(recovery_root.resolve())
    assert recovery.is_file()


def test_interrupted_manifest_needs_confirmation(knowledge_system, source_root):
    service = IntakeService(knowledge_system)
    plan = preview(service, source_root)
    plan["state"] = "running"
    service._save(plan)
    recovered = IntakeService(knowledge_system)
    assert recovered.read(plan["id"])["state"] == "interrupted"
    with pytest.raises(ValueError):
        recovered.run(plan["id"])


def test_invalid_or_unsupported_rules(knowledge_system, source_root):
    service = IntakeService(knowledge_system)
    with pytest.raises(ValueError):
        service.preview(IntakeRequest(path=str(source_root), rules={"documents": "full"}))
    with pytest.raises(ValueError):
        service.preview(IntakeRequest(path=str(source_root), exclusions=["../parent"]))


def test_only_verified_completed_backups_rotate(knowledge_system, source_root):
    from pathlib import Path

    (source_root / "a.md").write_text("first backup rotation")
    service = IntakeService(knowledge_system)
    first = execute(service, preview(service, source_root))
    first_backup = Path(first["recovery"])
    assert first_backup.exists()
    note = source_root / "a.md"
    note.write_text("second backup rotation")
    second = execute(service, preview(service, source_root))
    assert not first_backup.exists()
    assert Path(second["recovery"]).exists()
    assert second["superseded_recovery_bytes_released"] > 0
    assert service.read(first["id"])["recovery_retired_by"] == second["id"]
