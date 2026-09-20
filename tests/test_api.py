import json
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from pkas.api import create_app
from pkas.capability_registry import _stable_id
from pkas.config import Settings
from pkas.db import CURRENT_SCHEMA_VERSION, Database
from pkas.system import KnowledgeSystem

WEFLOW_FIXTURE = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"


def test_api_startup_initializes_main_database_once(test_settings: Settings, monkeypatch) -> None:
    original = Database.initialize
    calls: list[Database] = []

    def tracked(self: Database) -> None:
        calls.append(self)
        original(self)

    monkeypatch.setattr(Database, "initialize", tracked)
    with TestClient(create_app(test_settings)) as client:
        assert client.get("/api/runtime/ping").status_code == 200

    assert len(calls) == 1


def test_current_database_startup_skips_one_time_schema_migration(
    test_settings: Settings, monkeypatch
) -> None:
    database = Database(test_settings)
    database.initialize()

    def unexpected_schema_probe(*_args, **_kwargs) -> None:
        raise AssertionError("current database must not replay schema migration")

    monkeypatch.setattr(database, "_ensure_column", unexpected_schema_probe)
    database.initialize()

    with database.connect() as connection:
        version = connection.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    assert version == str(CURRENT_SCHEMA_VERSION)


def create_weflow_xlsx_export(source_root: Path) -> Path:
    xlsx = source_root / "api-customer.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["微信聊天记录"])
    sheet.append(["昵称", "API 测试客户", "微信ID", "wxid_api_customer"])
    sheet.append(
        [
            "序号",
            "时间",
            "发送者昵称",
            "发送者微信ID",
            "发送者备注",
            "发送者身份",
            "消息类型",
            "内容",
        ]
    )
    sheet.append(
        [
            1,
            "2025-02-05 10:00:00",
            "客户",
            "wxid_api_customer",
            "",
            "客户",
            "文本消息",
            "请提供报价。",
        ]
    )
    sheet.append([2, "2025-02-05 10:01:00", "我", "wxid_owner", "", "我", "文本消息", "收到。"])
    workbook.save(xlsx)
    records = source_root / "weflow-export-records.json"
    records.write_text(
        json.dumps(
            {
                "wxid_api_customer": [
                    {
                        "exportTime": 1738749660000,
                        "format": "xlsx",
                        "messageCount": 2,
                        "outputPath": str(xlsx.resolve()),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return records


def test_api_end_to_end(test_settings: Settings, source_root: Path) -> None:
    note = source_root / "customer.txt"
    note.write_text("客户要求所有结论都附带原始证据路径。", encoding="utf-8")
    report_dir = test_settings.data_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "rag-silver-gate-20990101-000000.json"
    report_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "hit_rate": 1.0,
                "diagnostics": {"passed": 1},
                "details": [{"case_id": "private-detail"}],
            }
        ),
        encoding="utf-8",
    )
    synthetic_runtime = test_settings.project_root / ".venv" / "Scripts" / "python.exe"
    synthetic_runtime.parent.mkdir(parents=True)
    synthetic_runtime.write_bytes(b"synthetic runtime")
    codex_config = test_settings.client_home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        '[mcp_servers.fixture]\ncommand = "python"\nenv = { TOKEN = "must-not-leak" }\n',
        encoding="utf-8",
    )

    with TestClient(create_app(test_settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        readiness = client.get("/api/core/readiness")
        assert readiness.status_code == 200
        readiness_data = readiness.json()["data"]
        assert readiness_data["claims"]["production_complete"] is False
        assert readiness_data["claims"]["cross_platform_complete"] is False
        capabilities = client.get("/api/capabilities/overview")
        assert capabilities.status_code == 200
        capability_data = capabilities.json()["data"]
        assert capability_data["platform"]["architecture"] == ("client-neutral-modular-monolith")
        assert capability_data["safety"]["secret_values_returned"] is False
        assert capability_data["safety"]["preview_required_before_writes"] is True
        client_configs = client.get("/api/capabilities/clients/config")
        assert client_configs.status_code == 200
        assert {item["id"] for item in client_configs.json()["data"]} == {
            "codex",
            "claude-desktop",
            "cursor",
        }
        server_id = _stable_id("codex", "fixture")
        mcp_preview = client.get(
            f"/api/capabilities/mcp/codex/{server_id}/preview"
        )
        assert mcp_preview.status_code == 200
        mcp_preview_data = mcp_preview.json()["data"]
        assert mcp_preview_data["requires_confirmation"] is True
        assert "must-not-leak" not in repr(mcp_preview.json())

        async def fake_probe(raw: dict[str, object], timeout: float) -> dict[str, object]:
            assert timeout == 12
            return {
                "server_name": "fixture",
                "server_version": "1",
                "protocol_version": "test",
                "tools": [],
                "resources": [],
                "prompts": [],
            }

        client.app.state.mcp_inspector._probe_runner = fake_probe
        refused_probe = client.post(
            f"/api/capabilities/mcp/codex/{server_id}/probe",
            json={"preview_token": mcp_preview_data["preview_token"], "confirmed": False},
        )
        assert refused_probe.status_code == 400
        mcp_probe = client.post(
            f"/api/capabilities/mcp/codex/{server_id}/probe",
            json={"preview_token": mcp_preview_data["preview_token"], "confirmed": True},
        )
        assert mcp_probe.status_code == 200
        assert mcp_probe.json()["data"]["process_reclaimed"] is True
        profile_payload = {
            "name": "API 开发 Profile",
            "description": "最小客户端无关能力集合",
            "client_id": "codex",
            "status": "active",
            "knowledge_domains": ["work"],
            "allowed_privacy": ["public", "private"],
            "daily_input_token_budget": 100000,
            "daily_output_token_budget": 10000,
            "bindings": [
                {"asset_kind": "skill", "asset_id": "api-skill", "enabled": True},
                {
                    "asset_kind": "mcp_server",
                    "asset_id": "personal_knowledge",
                    "enabled": False,
                },
            ],
        }
        created_profile = client.post("/api/capabilities/profiles", json=profile_payload)
        assert created_profile.status_code == 200
        created_profile_data = created_profile.json()["data"]
        assert created_profile_data["revision"] == 1
        assert created_profile_data["secret_values_returned"] is False
        profiles = client.get("/api/capabilities/profiles")
        assert profiles.status_code == 200
        assert profiles.json()["data"][0]["id"] == created_profile_data["id"]
        replaced_profile = client.put(
            f"/api/capabilities/profiles/{created_profile_data['id']}",
            json={
                **profile_payload,
                "description": "已更新",
                "expected_revision": 1,
            },
        )
        assert replaced_profile.status_code == 200
        assert replaced_profile.json()["data"]["revision"] == 2
        stale_profile = client.put(
            f"/api/capabilities/profiles/{created_profile_data['id']}",
            json={**profile_payload, "expected_revision": 1},
        )
        assert stale_profile.status_code == 409
        config_preview = client.post(
            "/api/capabilities/clients/codex/config/preview",
            json={"enabled": True},
        )
        assert config_preview.status_code == 200
        assert config_preview.json()["data"]["secret_values_returned"] is False
        rejected_apply = client.post(
            "/api/capabilities/clients/codex/config/apply",
            json={
                "enabled": True,
                "preview_token": config_preview.json()["data"]["preview_token"],
                "confirmed": False,
            },
        )
        assert rejected_apply.status_code == 400
        assert health.json()["status"] == "success"
        assert health.json()["data"]["backup"]["protocol"] == ("pkas-recovery-bundle-v1")
        assert health.json()["data"]["backup"]["secrets_included"] is False

        silver = client.get("/api/rag/eval/silver/latest")
        assert silver.status_code == 200
        assert "details" not in silver.json()["data"]
        silver_report = client.get("/api/rag/eval/silver/latest/report")
        assert silver_report.status_code == 200
        assert silver_report.headers["content-type"].startswith("application/json")
        assert silver_report.json()["details"][0]["case_id"] == "private-detail"

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

        debugged = client.post(
            "/api/rag/search-debug",
            json={"query": "原始证据路径", "domain": "work", "limit": 10},
        )
        assert debugged.status_code == 200
        assert debugged.json()["data"]["health"]["sufficiency"] == "sufficient"
        assert debugged.json()["data"]["results"][0]["parent_context"]["text"]

        document = client.get(f"/api/documents/{result['document_id']}")
        assert document.status_code == 200
        assert "所有结论" in document.json()["data"]["text"]

        context = client.post(
            "/api/agent/context",
            json={"task": "根据客户要求整理报告", "limit": 8},
        )
        assert context.status_code in {404, 405}

        dashboard = client.get("/api/dashboard").json()["data"]
        assert dashboard["counts"]["sources"] == 1
        assert dashboard["counts"]["knowledge_sources"] == 1
        assert dashboard["counts"]["knowledge_chunks"] == 1
        assert dashboard["counts"]["workflow_runs"] == 1
        assert dashboard["counts"]["agent_runs"] == 0


def test_rag_observability_and_persistent_eval_api(
    test_settings: Settings,
    source_root: Path,
) -> None:
    note = source_root / "rag-ground-truth.txt"
    note.write_text("MCP 服务端负责向客户端暴露可发现的知识检索工具。", encoding="utf-8")
    system = KnowledgeSystem.create(test_settings)
    system.database.initialize()
    imported = system.ingestion.import_file(note, domain="work", privacy="private")

    with TestClient(create_app(test_settings)) as client:
        status = client.get("/api/rag/status")
        assert status.status_code == 200
        assert status.json()["data"]["qdrant"]["status"] == "disabled"

        rejected = client.post(
            "/api/rag/eval/cases",
            json={
                "query": "未完成逐结果标注能否成为正式题",
                "expected_source_ids": [imported["source_id"]],
                "tags": ["gate"],
            },
        )
        assert rejected.status_code == 400
        assert "候选来源范围" in rejected.json()["detail"]

        created = client.post(
            "/api/rag/eval/cases",
            json={
                "query": "谁负责暴露知识检索工具",
                "expected_source_ids": [imported["source_id"]],
                "tags": ["mcp"],
                "judgment_scope_source_ids": [imported["source_id"]],
                "judgments": [
                    {
                        "source_id": imported["source_id"],
                        "relevance_grade": 3,
                        "judgment_basis": "human",
                    }
                ],
            },
        )
        assert created.status_code == 200
        assert created.json()["data"]["expected_source_ids"] == [imported["source_id"]]

        evaluated = client.post("/api/rag/eval/run", json={"top_k": 5})
        assert evaluated.status_code == 200
        assert evaluated.json()["data"]["case_count"] == 1
        assert evaluated.json()["data"]["hit_rate"] == 1.0
        assert evaluated.json()["data"]["recall_at_k"] == 1.0

        runs = client.get("/api/rag/eval/runs")
        assert runs.json()["data"][0]["retrieval_modes"] == {"fts_only": 1}

        draft = client.post(
            "/api/rag/eval/cases",
            json={
                "query": "MCP 服务端负责什么",
                "expected_source_ids": [imported["source_id"]],
                "tags": ["review-api"],
                "review_status": "draft",
            },
        ).json()["data"]
        context = client.get(
            f"/api/rag/eval/cases/{draft['id']}/review-context",
            params={"rerank_mode": "never"},
        )
        assert context.status_code == 200
        scope = context.json()["data"]["scope_source_ids"]
        assert imported["source_id"] in scope
        finalized = client.post(
            f"/api/rag/eval/cases/{draft['id']}/review",
            json={
                "judgment_scope_source_ids": scope,
                "judgments": [
                    {
                        "source_id": source_id,
                        "relevance_grade": 3,
                        "judgment_basis": "human",
                    }
                    for source_id in scope
                ],
                "category": "code",
                "difficulty": "normal",
                "match_policy": "any",
            },
        )
        assert finalized.status_code == 200
        assert finalized.json()["data"]["review_eligible"] is True
        progress = client.get("/api/rag/eval/review/progress").json()["data"]
        assert progress["eligible"] == 2
        assert progress["pending"] == 0

        rejected_draft = client.post(
            "/api/rag/eval/cases",
            json={
                "query": "这个含糊问题指什么",
                "expected_source_ids": [imported["source_id"]],
                "tags": ["reject-api"],
                "review_status": "draft",
            },
        ).json()["data"]
        rejected = client.post(
            f"/api/rag/eval/cases/{rejected_draft['id']}/reject",
            json={"reason_code": "ambiguous"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["data"]["review_status"] == "rejected"
        progress = client.get("/api/rag/eval/review/progress").json()["data"]
        assert progress["eligible"] == 2
        assert progress["rejected"] == 1
        assert progress["replacement_needed"] == 1


def test_legacy_agent_api_is_not_exposed(
    test_settings: Settings,
) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/agent/run",
            json={"task": "整理当前知识库开发状态", "persist_result": False},
        )
    assert response.status_code in {404, 405}


def test_sync_root_api_lists_refreshes_and_searches_catalog(
    test_settings: Settings,
    source_root: Path,
) -> None:
    (source_root / "existing-project.md").write_text("现成项目资料", encoding="utf-8")
    system = KnowledgeSystem.create(test_settings)
    root = system.sync.register_root(
        name="现成项目目录",
        root_path=str(source_root),
        connector_type="local_files",
        sync_mode="catalog",
    )

    with TestClient(create_app(test_settings)) as client:
        refreshed = client.post(f"/api/sync/roots/{root['id']}/scan")
        assert refreshed.status_code == 200
        assert refreshed.json()["data"]["files_seen"] == 1

        roots = client.get("/api/sync/roots").json()["data"]
        assert roots[0]["active_count"] == 1
        assert roots[0]["last_result"]["cataloged"] == 1

        searched = client.post(
            "/api/sync/catalog/search",
            json={"query": "existing-project", "root_id": root["id"], "limit": 20},
        )
        assert searched.status_code == 200
        assert searched.json()["data"][0]["relative_path"] == "existing-project.md"


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


def test_weflow_xlsx_export_api_flow(
    test_settings: Settings,
    source_root: Path,
) -> None:
    records = create_weflow_xlsx_export(source_root)

    with TestClient(create_app(test_settings)) as client:
        discovered = client.post(
            "/api/weflow/exports/discover",
            json={"records_path": str(records), "keyword": "", "limit": 100},
        )
        assert discovered.status_code == 200
        catalog = discovered.json()["data"]
        assert catalog["api_required"] is False
        assert catalog["existing_sessions"] == 1

        inspected = client.post(
            "/api/weflow/exports/inspect",
            json={
                "records_path": str(records),
                "session_ids": ["wxid_api_customer"],
            },
        )
        assert inspected.status_code == 200
        inspection = inspected.json()["data"]
        assert inspection["total_messages"] == 2

        imported = client.post(
            "/api/weflow/exports/import",
            json={
                "records_path": str(records),
                "session_ids": ["wxid_api_customer"],
                "inspection_token": inspection["inspection_token"],
                "privacy": "restricted",
            },
        )
        assert imported.status_code == 200
        assert imported.json()["data"]["result"]["imported"] == 2
        customers = client.get("/api/customers").json()["data"]
        assert customers[0]["display_name"] == "API 测试客户"


def test_weflow_manual_sync_status_api(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/weflow/manual-sync/status")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "manual_only"
    assert data["status"] == "idle"
    assert data["scheduled"] is False
    assert data["autostart"] is False


def test_personal_timeline_status_is_read_only(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/personal-timeline/status")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "manual_luna_build"
    assert data["pending_day_count"] == 0


def test_weflow_customer_api_end_to_end(
    test_settings: Settings,
    source_root: Path,
) -> None:
    chatlab = source_root / "customer-chatlab.json"
    chatlab.write_bytes(WEFLOW_FIXTURE.read_bytes())

    with TestClient(create_app(test_settings)) as client:
        inspection_response = client.post(
            "/api/weflow/chatlab/inspect",
            json={"path": str(chatlab), "session_id": None},
        )
        assert inspection_response.status_code == 200
        inspection = inspection_response.json()["data"]

        imported = client.post(
            "/api/weflow/chatlab/import",
            json={
                "path": str(chatlab),
                "session_id": None,
                "inspection_token": inspection["inspection_token"],
                "privacy": "restricted",
            },
        )
        assert imported.status_code == 200
        assert imported.json()["data"]["result"]["imported"] == 3

        customer = client.get("/api/customers").json()["data"][0]
        updated = client.post(
            f"/api/customers/{customer['id']}",
            json={
                "company": "示例门店公司",
                "stage": "delivery",
                "tags": ["门店", "数据看板"],
                "summary": "客户正在确认三家门店的数据看板范围。",
                "review_status": "approved",
            },
        ).json()["data"]
        assert updated["company"] == "示例门店公司"
        assert updated["stage"] == "delivery"
        hidden = client.get(f"/api/customers/{customer['id']}/timeline").json()["data"]
        visible = client.get(
            f"/api/customers/{customer['id']}/timeline",
            params={"include_restricted": "true"},
        ).json()["data"]
        assert hidden == []
        assert len(visible) == 3

        searched = client.post(
            "/api/customer-messages/search",
            json={
                "query": "报价单",
                "customer_id": customer["id"],
                "include_restricted": True,
            },
        )
        evidence = searched.json()["data"][0]
        assert evidence["platform_message_id"] == "wf-demo-001"

        signal = client.post(
            "/api/customer-signals",
            json={
                "customer_id": customer["id"],
                "signal_type": "commitment",
                "statement": "需要回复正式报价时间。",
                "evidence_message_ids": [evidence["message_id"]],
                "confidence": "high",
            },
        ).json()["data"]
        reviewed = client.post(
            f"/api/customer-signals/{signal['id']}/review",
            json={"decision": "approved", "reason": "消息证据已核对"},
        )
        assert reviewed.status_code == 200

        context = client.post(
            "/api/customer-reply/context",
            json={
                "customer_id": customer["id"],
                "task": "回复报价和门店看板问题",
                "include_restricted": True,
            },
        )
        assert context.status_code == 200
        assert len(context.json()["data"]["recent_messages"]) == 3
