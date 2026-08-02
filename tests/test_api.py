from pathlib import Path

from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.config import Settings


def test_api_end_to_end(test_settings: Settings, source_root: Path) -> None:
    note = source_root / "customer.txt"
    note.write_text("客户要求所有结论都附带原始证据路径。", encoding="utf-8")

    with TestClient(create_app(test_settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "success"

        inspection = client.post(
            "/api/import/inspect",
            json={"path": str(note), "recursive": False},
        )
        assert inspection.status_code == 200
        inspection_data = inspection.json()["data"]
        assert inspection_data["supported_files"] == 1

        imported = client.post(
            "/api/import/run",
            json={
                "path": str(note),
                "recursive": False,
                "domain": "work",
                "privacy": "private",
                "inspection_token": inspection_data["inspection_token"],
            },
        )
        assert imported.status_code == 200
        assert imported.json()["data"]["result"]["imported"] == 1

        searched = client.post(
            "/api/search",
            json={"query": "原始证据路径", "domain": "work", "limit": 10},
        )
        result = searched.json()["data"][0]
        assert result["original_uri"] == str(note.resolve())

        document = client.get(f"/api/documents/{result['document_id']}")
        assert document.status_code == 200
        assert "所有结论" in document.json()["data"]["text"]

        context = client.post(
            "/api/agent/context",
            json={"task": "根据客户要求整理报告", "limit": 8},
        )
        assert context.status_code == 200
        assert context.json()["data"]["selected_domain"] == "work"

        dashboard = client.get("/api/dashboard").json()["data"]
        assert dashboard["counts"]["sources"] == 1
        assert dashboard["counts"]["workflow_runs"] == 1
        assert dashboard["counts"]["agent_runs"] == 1


def test_api_rejects_knowledge_project_as_import_source(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/import/inspect",
            json={"path": str(test_settings.project_root), "recursive": True},
        )
    assert response.status_code == 400
    assert "不能作为导入来源" in response.json()["detail"]


def test_api_requires_current_inspection_token(
    test_settings: Settings,
    source_root: Path,
) -> None:
    note = source_root / "changing.txt"
    note.write_text("第一次检查", encoding="utf-8")

    with TestClient(create_app(test_settings)) as client:
        inspection = client.post(
            "/api/import/inspect",
            json={"path": str(note), "recursive": False},
        ).json()["data"]
        note.write_text("检查后内容发生变化", encoding="utf-8")
        response = client.post(
            "/api/import/run",
            json={
                "path": str(note),
                "recursive": False,
                "domain": "work",
                "privacy": "private",
                "inspection_token": inspection["inspection_token"],
            },
        )
        dashboard = client.get("/api/dashboard").json()["data"]

    assert response.status_code == 200
    assert response.json()["status"] == "warning"
    assert "发生了变化" in response.json()["data"]["error"]["message"]
    assert dashboard["counts"]["sources"] == 0
