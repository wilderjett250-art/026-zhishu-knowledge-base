import json

from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.catalog_classification import CatalogClassificationLedger
from pkas.project_map import (
    PROJECT_RESULT_SCHEMA,
    ProjectMapCreateRequest,
    ProjectMapPromotionRequest,
    ProjectMapRunRequest,
    ProjectMapService,
)


def _seed_ledger(settings):
    source_root = settings.project_root / "workspace"
    source_path = source_root / "demo" / "README.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("demo project", encoding="utf-8")
    ledger = CatalogClassificationLedger(settings.data_root)
    with ledger.connect() as db:
        record = {
            "relative": "README.md",
            "classification": {"label": "项目资料"},
            "understanding": {
                "origin": "agent",
                "protocol": "ai_understanding_v2",
                "purpose": "说明项目用途",
                "recommended_mode": "full",
            },
        }
        db.execute(
            "INSERT INTO entries(catalog_file_id,source_path,scope_path,catalog_category,"
            "byte_size,modified_ns,state,record,summary,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                str(source_path),
                str(source_root),
                "document",
                10,
                1,
                "inspected",
                json.dumps(record),
                json.dumps({"text": "项目说明"}),
                "now",
                "now",
            ),
        )


def test_project_map_uses_only_verified_derived_records(test_settings):
    _seed_ledger(test_settings)
    plan = ProjectMapService(test_settings).create(ProjectMapCreateRequest())
    assert plan["source"]["file_reading"] == "none"
    assert plan["source"]["main_knowledge_written"] is False
    assert plan["total_units"] == 1
    packet = ProjectMapService._packet(plan["units"])
    assert "source_path" not in json.dumps(packet)


def test_project_map_schema_is_closed():
    item = PROJECT_RESULT_SCHEMA["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert set(item["required"]) == {
        "id", "title", "what", "why", "how", "evidence_ids", "uncertainty"
    }


def test_project_map_rejection_keeps_only_safe_error_type(test_settings):
    _seed_ledger(test_settings)
    service = ProjectMapService(test_settings)
    plan = service.create(ProjectMapCreateRequest())

    class RejectedAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def complete(self, _packet):
            raise ValueError("synthetic provider detail")

    from unittest.mock import patch

    with patch("pkas.project_map.CodexAgent", RejectedAgent):
        result = service.run(
            plan["id"],
            ProjectMapRunRequest(confirmed=True, allow_remote_processing=True),
    )
    unit = result["units"][0]
    assert unit["state"] == "rejected"
    assert unit["rejection_reason"] == (
        "model_or_validation_rejected:ValueError/ValueError;single:ValueError"
    )


def test_project_map_batch_failure_falls_back_to_single_units(test_settings):
    _seed_ledger(test_settings)
    service = ProjectMapService(test_settings)
    plan = service.create(ProjectMapCreateRequest())
    second = json.loads(json.dumps(plan["units"][0]))
    second["id"] = "second-unit"
    second["materials"][0]["id"] = "file:2"
    plan["units"].append(second)
    service._save(plan)

    class IsolatedFallbackAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def complete(self, packet):
            if len(packet["items"]) > 1:
                raise ValueError("synthetic multi-item failure")
            unit = packet["items"][0]
            return (
                {
                    "items": [
                        {
                            "id": unit["id"],
                            "title": "恢复后的项目卡",
                            "what": "用于验证单项恢复",
                            "why": "避免批次连带失败",
                            "how": "逐项重试",
                            "evidence_ids": [unit["materials"][0]["id"]],
                            "uncertainty": "暂不明确",
                        }
                    ]
                },
                "thread-test",
            )

    from unittest.mock import patch

    with patch("pkas.project_map.CodexAgent", IsolatedFallbackAgent):
        result = service.run(
            plan["id"],
            ProjectMapRunRequest(confirmed=True, allow_remote_processing=True),
        )
    assert result["counts"] == {"done": 2}
    assert all(unit["overview"]["title"] == "恢复后的项目卡" for unit in result["units"])


def test_project_map_derives_local_fields_from_factual_response(test_settings):
    _seed_ledger(test_settings)
    service = ProjectMapService(test_settings)
    plan = service.create(ProjectMapCreateRequest())

    class AcceptedAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def complete(self, packet):
            unit_id = packet["items"][0]["id"]
            material_id = packet["items"][0]["materials"][0]["id"]
            return (
                {
                    "items": [
                        {
                            "id": unit_id,
                            "title": "演示项目",
                            "what": "用于演示",
                            "why": "用于验证流程",
                            "how": "通过说明文件组织",
                            "evidence_ids": [material_id],
                            "uncertainty": "暂不明确",
                        }
                    ]
                },
                "thread-test",
            )

    from unittest.mock import patch

    with patch("pkas.project_map.CodexAgent", AcceptedAgent):
        result = service.run(
            plan["id"],
            ProjectMapRunRequest(confirmed=True, allow_remote_processing=True),
        )
    overview = result["units"][0]["overview"]
    assert overview["project_type"] == "项目资料"
    assert overview["important_materials"] == ["README.md"]
    assert overview["confidence"] == "low"


def test_project_map_can_select_completed_project_materials(test_settings):
    _seed_ledger(test_settings)
    service = ProjectMapService(test_settings)
    plan = service.create(ProjectMapCreateRequest())
    plan["units"][0]["state"] = "done"
    service._save(plan)
    selection = service.promotion_selection(
        plan["id"],
        ProjectMapPromotionRequest(unit_ids=[plan["units"][0]["id"]], mode="full"),
    )
    assert selection["source_summary_job"] == f"project-map:{plan['id']}"
    assert [item["summary_file_id"] for item in selection["items"]] == ["catalog:1"]


def test_project_map_refresh_marks_changed_evidence_pending_without_replacing_plan(test_settings):
    _seed_ledger(test_settings)
    service = ProjectMapService(test_settings)
    plan = service.create(ProjectMapCreateRequest())
    plan["units"][0].update(state="done", overview={"title": "旧项目卡"})
    service._save(plan)
    source_path = test_settings.project_root / "workspace" / "demo" / "notes.md"
    source_path.write_text("new verified note", encoding="utf-8")
    ledger = CatalogClassificationLedger(test_settings.data_root)
    with ledger.connect() as db:
        record = {
            "relative": "notes.md",
            "classification": {"label": "项目资料"},
            "understanding": {
                "origin": "agent",
                "protocol": "ai_understanding_v2",
                "purpose": "补充项目说明",
                "recommended_mode": "full",
            },
        }
        db.execute(
            "INSERT INTO entries(catalog_file_id,source_path,scope_path,catalog_category,"
            "byte_size,modified_ns,state,record,summary,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                2,
                str(source_path),
                str(source_path.parents[1]),
                "document",
                10,
                2,
                "inspected",
                json.dumps(record),
                json.dumps({"text": "补充说明"}),
                "now",
                "now",
            ),
        )
    refreshed = service.refresh(plan["id"])
    assert refreshed["refresh_summary"] == {
        "added": 0,
        "changed": 1,
        "stale": 0,
        "unchanged": 0,
    }
    assert refreshed["units"][0]["state"] == "pending"
    assert refreshed["units"][0]["overview"] is None
    assert refreshed["units"][0]["evidence_file_count"] == 2


def test_project_map_refresh_route_returns_incremental_summary(test_settings):
    _seed_ledger(test_settings)
    plan = ProjectMapService(test_settings).create(ProjectMapCreateRequest())
    with TestClient(create_app(test_settings)) as client:
        response = client.post(f"/api/foundation/project-map/{plan['id']}/refresh")
    assert response.status_code == 200
    assert response.json()["data"]["refresh_summary"] == {
        "added": 0,
        "changed": 0,
        "stale": 0,
        "unchanged": 1,
    }


def test_project_map_promote_preview_route_uses_only_selected_materials(test_settings):
    _seed_ledger(test_settings)
    service = ProjectMapService(test_settings)
    plan = service.create(ProjectMapCreateRequest())
    plan["units"][0]["state"] = "done"
    service._save(plan)
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            f"/api/foundation/project-map/{plan['id']}/promote-preview",
            json={"unit_ids": [plan["units"][0]["id"]], "mode": "full"},
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "ready"
    assert data["request"]["source"] == "summary_job"
    assert len(data["items"]) == 1
