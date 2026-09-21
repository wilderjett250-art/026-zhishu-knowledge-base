from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pkas.capability_registry import _stable_id
from pkas.mcp_inspector import McpInspectorConflict, McpInspectorService


def write_config(home: Path, token: str = "must-not-leak") -> str:
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f'''[mcp_servers.local]
command = "python"
args = ["-m", "example"]
env = {{ TOKEN = "{token}" }}
''',
        encoding="utf-8",
    )
    return _stable_id("codex", "local")


@pytest.mark.asyncio
async def test_stdio_probe_completes_real_initialize_and_list_tools() -> None:
    result = await McpInspectorService._probe_stdio(
        {"command": sys.executable, "args": ["-m", "pkas.mcp_server"]},
        timeout_seconds=15,
    )

    assert result["server_name"] == "personal-knowledge-agent"
    assert any(item["name"] == "search_knowledge" for item in result["tools"])


@pytest.mark.asyncio
async def test_preview_is_secret_safe_and_probe_returns_capability_schema(tmp_path: Path) -> None:
    server_id = write_config(tmp_path)

    async def fake_probe(raw: dict[str, object], timeout: float) -> dict[str, object]:
        assert raw["env"] == {"TOKEN": "must-not-leak"}
        assert timeout == 12
        return {
            "server_name": "fixture",
            "server_version": "1.0",
            "protocol_version": "test",
            "tools": [
                {
                    "name": "search",
                    "description": "Search",
                    "input_schema": {"type": "object"},
                }
            ],
            "resources": [],
            "prompts": [],
        }

    service = McpInspectorService(tmp_path, probe_runner=fake_probe, signing_key=b"x" * 32)
    preview = service.preview("codex", server_id)

    assert preview["requires_confirmation"] is True
    assert preview["environment_variable_count"] == 1
    assert "must-not-leak" not in repr(preview)
    result = await service.probe("codex", server_id, preview_token=preview["preview_token"])
    assert result["tool_count"] == 1
    assert result["process_reclaimed"] is True
    assert result["secret_values_exposed"] is False
    assert "must-not-leak" not in repr(result)


@pytest.mark.asyncio
async def test_probe_rejects_stale_preview_after_config_change(tmp_path: Path) -> None:
    server_id = write_config(tmp_path)

    async def fake_probe(raw: dict[str, object], timeout: float) -> dict[str, object]:
        raise AssertionError("stale preview must not execute")

    service = McpInspectorService(tmp_path, probe_runner=fake_probe, signing_key=b"x" * 32)
    preview = service.preview("codex", server_id)
    write_config(tmp_path, token="changed")

    with pytest.raises(McpInspectorConflict):
        await service.probe("codex", server_id, preview_token=preview["preview_token"])
