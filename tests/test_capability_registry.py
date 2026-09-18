from pathlib import Path

from pkas.api import capability_rag_status
from pkas.capability_registry import CapabilityRegistry, CodexClientAdapter


def test_codex_adapter_discovers_metadata_without_exposing_config_values(tmp_path: Path) -> None:
    codex_root = tmp_path / ".codex"
    skill_root = codex_root / "skills" / "reviewer"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: reviewer\ndescription: Review code safely.\n---\nsecret instructions",
        encoding="utf-8",
    )
    (codex_root / "config.toml").write_text(
        """[mcp_servers.local]
command = "python"
env = { TOKEN = "must-not-leak" }

[mcp_servers.remote]
url = "https://example.invalid/mcp?token=must-not-leak"
enabled = false
""",
        encoding="utf-8",
    )

    snapshot = CodexClientAdapter().inspect(tmp_path)

    assert snapshot.client["detected"] is True
    assert snapshot.client["skill_count"] == 1
    assert snapshot.skills[0]["name"] == "reviewer"
    assert snapshot.skills[0]["description"] == "Review code safely."
    assert snapshot.skills[0]["validation"] == "valid"
    assert len(snapshot.skills[0]["content_hash"]) == 64
    assert snapshot.skills[0]["total_bytes"] > 0
    assert {item["transport"] for item in snapshot.mcp_servers} == {
        "stdio",
        "streamable-http",
    }
    assert all(not item["secret_values_exposed"] for item in snapshot.mcp_servers)
    assert all("config_keys" in item for item in snapshot.mcp_servers)
    local = next(item for item in snapshot.mcp_servers if item["name"] == "local")
    assert local["probe_supported"] is True
    assert "must-not-leak" not in repr(snapshot)


def test_capability_registry_is_client_neutral_and_transactional(tmp_path: Path) -> None:
    overview = CapabilityRegistry(tmp_path).overview(
        knowledge_stats={"counts": {"knowledge_chunks": 12, "workflow_runs": 2}},
        rag_status={"qdrant": {"status": "ready", "points": 9}, "mcp_enabled": False},
    )

    assert overview["platform"]["architecture"] == "client-neutral-modular-monolith"
    assert overview["summary"]["clients"] == 3
    assert overview["safety"]["configuration_writes_enabled"] is True
    assert overview["safety"]["preview_required_before_writes"] is True
    assert overview["safety"]["automatic_rollback_on_failure"] is True
    assert overview["safety"]["secret_values_returned"] is False
    assert overview["interfaces"][1]["status"] == "paused"
    assert overview["services"][1]["records"] == 9


def test_capability_rag_status_degrades_without_qdrant(knowledge_system) -> None:
    # The developer workstation may have the real Qdrant sidecar listening on
    # the default port. Point this test at an intentionally closed local port
    # so it exercises the offline branch independently of host runtime state.
    knowledge_system.settings.qdrant_url = "http://127.0.0.1:1"
    status = capability_rag_status(knowledge_system)

    assert status["qdrant"]["status"] == "offline"
    assert status["qdrant"]["points"] == 0
