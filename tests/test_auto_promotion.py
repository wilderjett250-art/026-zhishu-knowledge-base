from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.auto_promotion import (
    PROMOTION_RESULT_SCHEMA,
    AutoPromotionRequest,
    AutoPromotionRunRequest,
    AutoPromotionService,
)
from pkas.directory_summary import (
    CatalogOverviewRequest,
    DirectorySummaryRunRequest,
    DirectorySummaryService,
)


def _catalog_with_groups(test_settings):
    import sqlite3

    catalog = test_settings.data_root / "machine-catalog" / "catalog.sqlite"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(catalog) as db:
        db.execute(
            """CREATE TABLE files(
                scope_path TEXT,top_group TEXT,state TEXT,byte_size INTEGER,
                extension TEXT,category TEXT)"""
        )
        db.executemany(
            "INSERT INTO files VALUES(?,?,?,?,?,?)",
            [
                ("E:\\", "Docs", "active", 20, ".md", "documents"),
                ("E:\\", "Docs", "active", 30, ".pdf", "documents"),
                ("E:\\", "Media", "active", 40, ".mp4", "media"),
            ],
        )


def test_auto_promotion_schema_required_fields_match_properties():
    item = PROMOTION_RESULT_SCHEMA["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"])


def test_latest_auto_promotion_endpoint_does_not_match_dynamic_id_route(test_settings):
    service = AutoPromotionService(test_settings)
    service._save(
        {
            "id": "a" * 32,
            "kind": "catalog_auto_promotion",
            "state": "done",
            "created_at": "2026-09-14T00:00:00+00:00",
            "units": [],
        }
    )
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/foundation/auto-promotion/latest")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == "a" * 32


def test_auto_promotion_uses_luna_but_cannot_write_knowledge(test_settings, monkeypatch):
    _catalog_with_groups(test_settings)

    class OverviewAgent:
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
                        "id": item["id"], "purpose": "资料", "summary": "目录统计概览",
                        "topics": ["资料"], "evidence_id": "directory", "uncertainty": "未读正文。",
                    }
                    for item in packet["items"]
                ]
            }, "overview-thread"

    class PromotionAgent(OverviewAgent):
        def complete(self, packet):
            values = []
            for item in packet["items"]:
                mode = "catalog" if item["hard_cap"] == "catalog" else "semantic"
                values.append(
                    {
                        "id": item["id"], "recommended_mode": mode,
                        "rationale": "仅生成候选层级。", "evidence_id": "hard_policy",
                        "uncertainty": "需要逐个文件检查。",
                        "needs_file_inspection": mode != "catalog",
                    }
                )
            return {"items": values}, "promotion-thread"

    monkeypatch.setattr("pkas.directory_summary.CodexAgent", OverviewAgent)
    overview_service = DirectorySummaryService(test_settings)
    overview = overview_service.create_catalog_overview(CatalogOverviewRequest())
    overview_service.run(
        overview["id"], DirectorySummaryRunRequest(confirmed=True, allow_remote_processing=True)
    )
    monkeypatch.setattr("pkas.auto_promotion.CodexAgent", PromotionAgent)
    service = AutoPromotionService(test_settings)
    plan = service.create(AutoPromotionRequest(overview_id=overview["id"]))
    result = service.run(
        plan["id"], AutoPromotionRunRequest(confirmed=True, allow_remote_processing=True)
    )

    assert result["state"] == "done"
    assert service.latest()["id"] == plan["id"]
    stored = service.read(plan["id"])
    assert stored["source"]["file_reading"] == "none"
    assert stored["remote_called"] is True
    assert {item["recommendation"]["recommended_mode"] for item in stored["units"]} == {
        "catalog", "semantic"
    }
    semantic = next(
        item
        for item in stored["units"]
        if item["recommendation"]["recommended_mode"] == "semantic"
    )
    assert semantic["recommendation"]["needs_file_inspection"] is True
    assert semantic["recommendation"]["candidate_intake_action"] == "semantic"


def test_auto_promotion_rejects_model_above_hard_cap(test_settings):
    packet = {
        "items": [{
            "id": "one", "hard_cap": "catalog",
            "evidence_options": [{"id": "safe", "text": "x"}],
        }]
    }
    result = {"items": [{
        "id": "one", "recommended_mode": "semantic", "rationale": "x",
        "evidence_id": "safe", "uncertainty": "x", "needs_file_inspection": True,
    }]}
    import pytest
    with pytest.raises(ValueError, match="安全上限"):
        AutoPromotionService._validate(packet, result)
