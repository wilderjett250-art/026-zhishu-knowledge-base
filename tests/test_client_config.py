from __future__ import annotations

import json
from pathlib import Path

import pytest

from pkas.client_config import (
    ClientConfigConflict,
    ClientConfigTransactionService,
    ClientConnectionError,
)
from pkas.config import Settings


class FakeTransactionService(ClientConfigTransactionService):
    async def _probe_mcp(self) -> dict[str, object]:
        return {"status": "passed", "server": "personal-knowledge-agent", "tools": 26}


class FailingTransactionService(ClientConfigTransactionService):
    async def _probe_mcp(self) -> dict[str, object]:
        raise ClientConnectionError("probe failed")


@pytest.fixture
def transaction_settings(tmp_path: Path) -> Settings:
    project_root = tmp_path / "pkas"
    runtime = project_root / ".venv" / "Scripts"
    runtime.mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"synthetic runtime")
    settings = Settings(
        project_root=project_root,
        data_root=project_root / "data",
        integration_home=tmp_path / "home",
        deepseek_api_key=None,
        embedding_api_key=None,
    )
    settings.ensure_directories()
    return settings


def service(
    cls: type[ClientConfigTransactionService],
    settings: Settings,
) -> ClientConfigTransactionService:
    return cls(
        settings,
        settings.integration_home or Path.home(),
        protect=lambda value: b"protected:" + value,
        unprotect=lambda value: value.removeprefix(b"protected:"),
    )


def test_preview_is_redacted_and_does_not_write(transaction_settings: Settings) -> None:
    config = transaction_settings.integration_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.other]\ncommand = "node"\nenv = { TOKEN = "must-not-leak" }\n',
        encoding="utf-8",
    )
    original = config.read_bytes()

    preview = service(FakeTransactionService, transaction_settings).preview(
        "codex", enabled=True
    )

    assert preview["changed"] is True
    assert preview["secret_values_returned"] is False
    assert "must-not-leak" not in repr(preview)
    assert "<redacted>" in repr(preview["redacted_diff"])
    assert config.read_bytes() == original


@pytest.mark.asyncio
async def test_codex_apply_is_atomic_and_rollback_backup_restores(
    transaction_settings: Settings,
) -> None:
    config = transaction_settings.integration_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    original = b'theme = "dark"\n'
    config.write_bytes(original)
    manager = service(FakeTransactionService, transaction_settings)
    preview = manager.preview("codex", enabled=True)

    result = await manager.apply(
        "codex",
        enabled=True,
        preview_token=preview["preview_token"],
    )

    assert result["connection"]["status"] == "passed"
    assert b"personal_knowledge" in config.read_bytes()
    assert result["secret_values_returned"] is False
    restored = manager.rollback("codex", result["backup_id"])
    assert restored["status"] == "restored"
    assert config.read_bytes() == original


@pytest.mark.asyncio
async def test_apply_rejects_stale_preview(transaction_settings: Settings) -> None:
    config = transaction_settings.integration_home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "one"\n', encoding="utf-8")
    manager = service(FakeTransactionService, transaction_settings)
    preview = manager.preview("codex", enabled=True)
    config.write_text('model = "two"\n', encoding="utf-8")

    with pytest.raises(ClientConfigConflict):
        await manager.apply(
            "codex",
            enabled=True,
            preview_token=preview["preview_token"],
        )

    assert config.read_text(encoding="utf-8") == 'model = "two"\n'


@pytest.mark.asyncio
async def test_connection_failure_restores_original_config(transaction_settings: Settings) -> None:
    config = transaction_settings.integration_home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    original = json.dumps(
        {"mcpServers": {"other": {"command": "node", "env": {"TOKEN": "secret"}}}}
    ).encode()
    config.write_bytes(original)
    manager = service(FailingTransactionService, transaction_settings)
    preview = manager.preview("cursor", enabled=True)

    with pytest.raises(ClientConnectionError):
        await manager.apply(
            "cursor",
            enabled=True,
            preview_token=preview["preview_token"],
        )

    assert config.read_bytes() == original
    assert manager.list_backups("cursor")


@pytest.mark.asyncio
async def test_json_clients_preserve_unrelated_servers(transaction_settings: Settings) -> None:
    config = (
        transaction_settings.integration_home
        / "AppData"
        / "Roaming"
        / "Claude"
        / "claude_desktop_config.json"
    )
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"existing": {"url": "https://example.invalid"}}}),
        encoding="utf-8",
    )
    manager = service(FakeTransactionService, transaction_settings)
    enable = manager.preview("claude-desktop", enabled=True)
    await manager.apply(
        "claude-desktop", enabled=True, preview_token=enable["preview_token"]
    )
    configured = json.loads(config.read_text(encoding="utf-8"))
    assert set(configured["mcpServers"]) == {"existing", "personal_knowledge"}

    disable = manager.preview("claude-desktop", enabled=False)
    await manager.apply(
        "claude-desktop", enabled=False, preview_token=disable["preview_token"]
    )
    configured = json.loads(config.read_text(encoding="utf-8"))
    assert set(configured["mcpServers"]) == {"existing"}
