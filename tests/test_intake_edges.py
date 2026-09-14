import pytest
from test_intake import execute, preview

from pkas.intake import IntakeRequest, IntakeService


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
