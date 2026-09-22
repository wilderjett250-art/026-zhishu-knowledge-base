import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pkas.machine_catalog import MachineCatalog, MachineCatalogConflict


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_machine_catalog_is_separate_resumable_and_generates_directory_md(
    test_settings, tmp_path: Path
) -> None:
    source = tmp_path / "full-drive-sample"
    nested = source / "project-alpha"
    nested.mkdir(parents=True)
    document = nested / "客户项目说明.docx"
    image = nested / "现场截图.png"
    secret = nested / "private.key"
    document.write_bytes(b"not-a-real-docx-but-catalog-does-not-parse")
    image.write_bytes(b"\x89PNG\r\n\x1a\nmetadata-only")
    secret.write_text("never catalog", encoding="utf-8")
    before = {path: digest(path) for path in (document, image, secret)}

    service = MachineCatalog(test_settings)
    service._excluded_roots = []  # isolate product-level backup/test exclusions from fixture
    job = service.start(
        confirmed=True,
        scopes=[{"path": str(source), "kind": "fixed_data_drive"}],
    )
    deadline = time.monotonic() + 10
    while job["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        job = service.view(job["id"])

    assert job["state"] == "completed"
    assert job["catalog_files"] == 2
    assert job["summary_files"] == 1
    assert service.integrity() == {
        "quick_check": "ok",
        "files": 2,
        "fts_rows": 2,
        "images_deferred": 1,
    }
    results = service.search("客户项目")
    assert results[0]["title"] == document.name
    assert results[0]["retrieval_channels"] == ["catalog"]
    summaries = list(Path(job["summary_path"]).rglob("*.md"))
    assert len(summaries) == 1
    assert "没有读取正文" in summaries[0].read_text(encoding="utf-8")
    assert {path: digest(path) for path in before} == before
    service.close()


def test_machine_catalog_requires_confirmation(test_settings, tmp_path: Path) -> None:
    source = tmp_path / "scope"
    source.mkdir()
    service = MachineCatalog(test_settings)
    try:
        service.start(
            confirmed=False,
            scopes=[{"path": str(source), "kind": "fixed_data_drive"}],
        )
    except ValueError as exc:
        assert "明确确认" in str(exc)
    else:
        raise AssertionError("unconfirmed machine scan was accepted")


def test_pause_only_changes_the_requested_catalog_job(test_settings, tmp_path: Path) -> None:
    service = MachineCatalog(test_settings)
    timestamp = datetime.now(UTC).isoformat()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    with service.connect() as connection:
        for job_id in ("first", "second"):
            connection.execute(
                """INSERT INTO jobs(id,state,stage,started_at,updated_at,message,owner_pid)
                VALUES(?, 'running', 'catalog', ?, ?, '', NULL)""",
                (job_id, timestamp, timestamp),
            )
        for job_id, root in (("first", first_root), ("second", second_root)):
            scope_id = f"scope-{job_id}"
            connection.execute(
                "INSERT INTO scopes(id,job_id,root_path,kind) VALUES(?,?,?,?)",
                (scope_id, job_id, str(root), "fixture"),
            )
            connection.execute(
                """INSERT INTO directory_queue(job_id,scope_id,path,state)
                VALUES(?,?,?,'working')""",
                (job_id, scope_id, str(root)),
            )
        connection.commit()

    paused = service.pause("first")

    assert paused["state"] == "paused"
    with service.connect() as connection:
        second_state = connection.execute(
            "SELECT state FROM jobs WHERE id='second'"
        ).fetchone()[0]
        assert second_state == "running"
        assert (
            connection.execute(
                "SELECT state FROM directory_queue WHERE job_id='first'"
            ).fetchone()[0]
            == "pending"
        )
        assert (
            connection.execute(
                "SELECT state FROM directory_queue WHERE job_id='second'"
            ).fetchone()[0]
            == "working"
        )
    service.close()


def test_pause_rejects_a_different_active_catalog_job(test_settings, tmp_path: Path) -> None:
    service = MachineCatalog(test_settings)
    timestamp = datetime.now(UTC).isoformat()
    with service.connect() as connection:
        connection.execute(
            """INSERT INTO jobs(id,state,stage,started_at,updated_at,message,owner_pid)
            VALUES('target', 'paused', 'catalog', ?, ?, '', NULL)""",
            (timestamp, timestamp),
        )
        connection.commit()

    class ActiveThread:
        def is_alive(self) -> bool:
            return True

    service._thread = ActiveThread()  # type: ignore[assignment]
    service._active_job_id = "other"

    with pytest.raises(MachineCatalogConflict, match="另一项A库索引"):
        service.pause("target")
    assert service._stop.is_set() is False
