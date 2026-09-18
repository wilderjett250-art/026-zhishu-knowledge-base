import pytest
from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.directory_summary import (
    DIRECTORY_RESULT_SCHEMA,
    CatalogOverviewRequest,
    DirectorySummaryRequest,
    DirectorySummaryRunRequest,
    DirectorySummaryService,
)


def test_directory_overview_schema_required_fields_match_properties():
    item = DIRECTORY_RESULT_SCHEMA["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"])


def _catalog_with_groups(test_settings):
    catalog = test_settings.data_root / "machine-catalog" / "catalog.sqlite"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3

    with sqlite3.connect(catalog) as db:
        db.execute(
            """CREATE TABLE files(
                scope_path TEXT,top_group TEXT,state TEXT,byte_size INTEGER,
                extension TEXT,category TEXT)"""
        )
        db.executemany(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            [
                ("E:\\", "Alpha", "active", 20, ".py", "code"),
                ("E:\\", "Alpha", "active", 30, ".md", "documents"),
                ("E:\\", "Beta", "active", 40, ".pdf", "documents"),
            ],
        )


def test_catalog_overview_reuses_a_catalog_and_luna_results_are_provenanced(
    test_settings, monkeypatch
):
    _catalog_with_groups(test_settings)

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def complete(self, packet):
            return {
                "items": [
                    {
                        "id": item["id"],
                        "purpose": "待复核的资料目录",
                        "summary": "仅依据目录级索引统计生成概览。",
                        "topics": ["资料"],
                        "evidence_id": "directory",
                        "uncertainty": "未读取文件正文。",
                    }
                    for item in packet["items"]
                ]
            }, "fake-thread"

    monkeypatch.setattr("pkas.directory_summary.CodexAgent", FakeAgent)
    service = DirectorySummaryService(test_settings)
    plan = service.create_catalog_overview(CatalogOverviewRequest(max_units_per_run=10))

    assert plan["source"]["file_reading"] == "none"
    assert plan["source"]["catalog_file_count"] == 3
    assert plan["total_units"] == 2
    probe = service._overview_packet(service.read(plan["id"])["units"])
    assert DirectorySummaryService._validate_overview_result(
        probe, FakeAgent().complete(probe)[0]
    )
    result = service.run(
        plan["id"], DirectorySummaryRunRequest(confirmed=True, allow_remote_processing=True)
    )

    assert result["completed_units"] == 2
    stored = service.read(plan["id"])
    assert stored["remote_called"] is True
    assert stored["units"][0]["summary"]["confidence"] == "directory_index_only"
    assert stored["units"][0]["thread_id"] == "fake-thread"


def test_preview_is_metadata_only_and_groups_first_level_directories(
    test_settings, source_root, monkeypatch
):
    alpha = source_root / "Alpha Project"
    beta = source_root / "Beta"
    alpha.mkdir()
    beta.mkdir()
    files = [alpha / "brief.docx", alpha / "app.py", beta / "photo.jpg"]
    for path in files:
        path.write_text("x", encoding="utf-8")

    monkeypatch.setattr("pkas.directory_summary.authorize_root", lambda _: source_root.resolve())
    monkeypatch.setattr("pkas.directory_summary.everything_scan", lambda *args: iter(files))
    monkeypatch.setattr("pkas.directory_summary.is_sensitive_path", lambda _: False)
    monkeypatch.setattr("pkas.directory_summary.linked", lambda _: False)
    monkeypatch.setattr("pkas.directory_summary.EXCLUDED", set())
    request = DirectorySummaryRequest(path=str(source_root))
    plan = DirectorySummaryService(test_settings).preview(request)

    assert plan["cloud_called"] is False
    assert plan["source_files_written"] == 0
    expected_export = "derived_only" if source_root.drive.casefold() == "c:" else "requires_review"
    assert plan["source_export_state"] == expected_export
    assert plan["discovered_files"] == 3
    assert plan["inspected_files"] == 3
    assert [item["name"] for item in plan["units"]] == ["Alpha Project", "Beta"]
    assert plan["units"][0]["categories"] == {"documents": 1, "code": 1}


def test_run_fails_closed_without_explicit_remote_consent(
    test_settings, source_root, monkeypatch
):
    file = source_root / "one.md"
    file.write_text("x", encoding="utf-8")
    monkeypatch.setattr("pkas.directory_summary.authorize_root", lambda _: source_root.resolve())
    monkeypatch.setattr("pkas.directory_summary.everything_scan", lambda *args: iter([file]))
    service = DirectorySummaryService(test_settings)
    plan = service.preview(DirectorySummaryRequest(path=str(source_root)))
    with pytest.raises(ValueError, match="确认"):
        service.run(
            plan["id"], DirectorySummaryRunRequest(confirmed=False, allow_remote_processing=False)
        )
    with pytest.raises(ValueError, match="云端处理"):
        service.run(
            plan["id"], DirectorySummaryRunRequest(confirmed=True, allow_remote_processing=False)
        )


def test_confirmed_run_calls_luna_and_writes_derived_md(
    test_settings, source_root, monkeypatch
):
    file = source_root / "project.md"
    file.write_text("这是一个采集项目说明", encoding="utf-8")
    monkeypatch.setattr("pkas.directory_summary.authorize_root", lambda _: source_root.resolve())
    monkeypatch.setattr("pkas.directory_summary.everything_scan", lambda *args: iter([file]))
    monkeypatch.setattr("pkas.directory_summary.is_sensitive_path", lambda _: False)
    monkeypatch.setattr("pkas.directory_summary.linked", lambda _: False)
    monkeypatch.setattr("pkas.directory_summary.EXCLUDED", set())

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def complete(self, packet):
            item = packet["items"][0]
            return {
                "items": [{
                    "id": item["id"],
                    "purpose": "采集项目资料",
                    "summary": "根据有限文件抽样生成的待复核摘要。",
                    "topics": ["采集"],
                    "evidence_id": "file_count",
                    "uncertainty": "只读取了有限抽样，尚未全文理解。",
                }]
            }, "sample-thread"

    monkeypatch.setattr("pkas.directory_summary.CodexAgent", FakeAgent)
    service = DirectorySummaryService(test_settings)
    plan = service.preview(DirectorySummaryRequest(path=str(source_root)))
    result = service.run(
        plan["id"], DirectorySummaryRunRequest(
            confirmed=True, allow_remote_processing=True, max_units=10
        )
    )

    assert result["state"] == "done"
    assert result["cloud_called"] is True
    assert result["derived_md_files_written"] == 1
    stored = service.read(plan["id"])
    md = stored["units"][0]["derived_md_path"]
    assert md.endswith(".md")
    with open(md, encoding="utf-8") as handle:
        assert "采集项目资料" in handle.read()
    assert stored["source_files_written"] == 0


def test_api_reports_default_off_and_rejects_unknown_plan(test_settings):
    with TestClient(create_app(test_settings)) as client:
        status = client.get("/api/foundation/directory-summaries")
        assert status.status_code == 200
        assert status.json()["data"]["remote_enabled"] is False
        response = client.post(
            "/api/foundation/directory-summaries/" + "0" * 32 + "/run",
            json={"confirmed": True, "allow_remote_processing": True},
        )
        assert response.status_code in {404, 409}
