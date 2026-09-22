import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from pkas.api import create_app
from pkas.config import Settings
from pkas.system import KnowledgeSystem


def _create_legacy_weflow_layout(source_root: Path) -> tuple[Path, Path]:
    root = source_root / "legacy-weflow"
    for path in (
        root / "package.json",
        root / "node_modules" / "electron" / "dist" / "electron.exe",
        root / "node_modules" / "electron-store" / "index.js",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    records = source_root / "weflow-export-records.json"
    records.write_text("{}", encoding="utf-8")
    return root, records


def test_provider_registry_exposes_safe_builtin_routes(
    knowledge_system: KnowledgeSystem,
) -> None:
    providers = knowledge_system.import_providers.list_providers()
    by_id = {item["id"]: item for item in providers}

    assert set(by_id) == {
        "local-files",
        "obsidian-vault",
        "weflow-legacy-export",
        "chatlab-file",
    }
    assert by_id["weflow-legacy-export"]["delivery"] == "bring_your_own_exporter"
    assert by_id["weflow-legacy-export"]["requires_external_app"] is True
    assert by_id["chatlab-file"]["requires_external_app"] is False
    assert by_id["chatlab-file"]["actions"] == [
        "/api/chat-imports/chatlab/inspect",
        "/api/chat-imports/chatlab/import",
    ]
    assert by_id["obsidian-vault"]["supports_background_sync"] is False


def test_weflow_legacy_probe_reads_only_installation_metadata(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
    monkeypatch,
) -> None:
    root, records = _create_legacy_weflow_layout(source_root)

    def unexpected_export_parse(**_kwargs):
        raise AssertionError("provider probe must not parse export records")

    monkeypatch.setattr(knowledge_system.weflow, "discover_exports", unexpected_export_parse)
    result = knowledge_system.import_providers.probe(
        "weflow-legacy-export",
        records_path=str(records),
        weflow_root=str(root),
    )

    assert result["status"] == "existing_exports_ready"
    assert result["content_read"] is False
    assert result["key_accessed"] is False
    assert result["database_accessed"] is False
    assert result["export_index_present"] is True
    assert result["legacy_runtime_detected"] is True
    assert result["manual_sync_eligible"] is (os.name == "nt")
    assert str(records) not in json.dumps(result, ensure_ascii=False)
    assert str(root) not in json.dumps(result, ensure_ascii=False)


def test_obsidian_provider_registers_without_scanning_and_skips_internal_folders(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    vault = source_root / "notes"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".trash").mkdir()
    (vault / ".git").mkdir()
    (vault / "project").mkdir()
    (vault / "note.md").write_text("# 可索引笔记", encoding="utf-8")
    (vault / ".obsidian" / "workspace.md").write_text("不要索引", encoding="utf-8")
    (vault / ".trash" / "removed.md").write_text("不要索引", encoding="utf-8")
    (vault / ".git" / "ignored.md").write_text("不要索引", encoding="utf-8")
    (vault / "project" / "plan.md").write_text("# 项目计划", encoding="utf-8")

    probe = knowledge_system.import_providers.probe("obsidian-vault", location=str(vault))
    assert probe["status"] == "ready"
    assert probe["content_read"] is False
    assert probe["top_level_markdown_files"] == 1

    registered = knowledge_system.import_providers.register_obsidian_vault(
        vault_path=str(vault),
        name="测试笔记库",
        domain="work",
        privacy="private",
        sync_mode="catalog",
        recursive=True,
    )
    root = registered["root"]
    assert registered["scan_started"] is False
    assert root["connector_type"] == "obsidian_vault"
    assert root["config"]["excluded_directories"] == [".obsidian", ".trash", ".git"]

    scan = knowledge_system.sync.scan_root(root["id"])
    assert scan["files_seen"] == 2
    catalog = knowledge_system.sync.search_catalog(".md", root_id=root["id"])
    assert {item["relative_path"] for item in catalog} == {"note.md", "project\\plan.md"}


def test_chatlab_accepts_an_explicit_non_weflow_file_generator(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["chatlab"]["generator"] = "ExampleChatExporter"
    target = source_root / "example-chatlab.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    inspection = knowledge_system.weflow.inspect_chatlab_file(str(target))
    result = knowledge_system.weflow.import_chatlab_file(
        path=str(target),
        inspection_token=inspection["inspection_token"],
        privacy="restricted",
    )

    assert result["imported"] == 3
    with knowledge_system.database.connect() as connection:
        connector = connection.execute(
            "SELECT connector_type, config_json FROM connectors WHERE connector_type = ?",
            ("chatlab-wechat-file",),
        ).fetchone()
    assert connector is not None
    assert "ExampleChatExporter" in connector["config_json"]


def test_import_provider_api_exposes_metadata_probe_and_confirmed_obsidian_registration(
    test_settings: Settings,
    source_root: Path,
) -> None:
    vault = source_root / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "home.md").write_text("# Home", encoding="utf-8")

    with TestClient(create_app(test_settings)) as client:
        providers = client.get("/api/import-providers")
        assert providers.status_code == 200
        assert {item["id"] for item in providers.json()["data"]} >= {
            "obsidian-vault",
            "chatlab-file",
        }

        probe = client.post(
            "/api/import-providers/obsidian-vault/probe",
            json={"location": str(vault)},
        )
        assert probe.status_code == 200
        assert probe.json()["data"]["status"] == "ready"
        assert probe.json()["data"]["content_read"] is False

        registered = client.post(
            "/api/import-providers/obsidian-vault/register",
            json={
                "vault_path": str(vault),
                "name": "API Vault",
                "domain": "work",
                "privacy": "private",
                "sync_mode": "catalog",
                "recursive": True,
                "confirmed": True,
            },
        )
        assert registered.status_code == 200
        assert registered.json()["data"]["scan_started"] is False

        missing_confirmation = client.post(
            "/api/import-providers/obsidian-vault/register",
            json={"vault_path": str(vault)},
        )
        assert missing_confirmation.status_code == 422


def test_chatlab_generic_routes_keep_the_weflow_alias_compatible(
    test_settings: Settings,
    source_root: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"
    chatlab = source_root / "chatlab.json"
    chatlab.write_bytes(fixture.read_bytes())

    with TestClient(create_app(test_settings)) as client:
        generic = client.post(
            "/api/chat-imports/chatlab/inspect",
            json={"path": str(chatlab), "session_id": None},
        )
        legacy = client.post(
            "/api/weflow/chatlab/inspect",
            json={"path": str(chatlab), "session_id": None},
        )

    assert generic.status_code == 200
    assert legacy.status_code == 200
    assert generic.json()["data"]["inspection_token"] == legacy.json()["data"]["inspection_token"]
