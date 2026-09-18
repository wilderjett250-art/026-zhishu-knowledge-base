from __future__ import annotations

import hashlib
import hmac
import io
import json
import secrets
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from pkas.capability_registry import _stable_id


class McpInspectorError(RuntimeError):
    pass


class McpInspectorConflict(McpInspectorError):
    pass


ProbeRunner = Callable[[dict[str, Any], float], Awaitable[dict[str, Any]]]


class McpInspectorService:
    """Explicit-confirm, secret-safe one-shot inspection of configured MCP servers."""

    def __init__(
        self,
        home: Path,
        *,
        probe_runner: ProbeRunner | None = None,
        signing_key: bytes | None = None,
    ) -> None:
        self.home = home
        self._probe_runner = probe_runner or self._probe_stdio
        self._signing_key = signing_key or secrets.token_bytes(32)
        self._latest: dict[str, dict[str, Any]] = {}

    def preview(self, client_id: str, server_id: str) -> dict[str, Any]:
        record = self._resolve(client_id, server_id)
        raw = record.pop("raw")
        fingerprint = self._fingerprint(raw)
        return {
            **record,
            "preview_token": self._token(client_id, server_id, fingerprint),
            "config_fingerprint": fingerprint[:12],
            "action": "启动一次、只读取MCP能力清单、随后关闭并回收进程",
            "timeout_seconds": 12,
            "requires_confirmation": True,
            "secret_values_exposed": False,
            "latest_probe": self._latest.get(server_id),
        }

    async def probe(
        self,
        client_id: str,
        server_id: str,
        *,
        preview_token: str,
        timeout_seconds: float = 12,
    ) -> dict[str, Any]:
        record = self._resolve(client_id, server_id)
        raw = record.pop("raw")
        fingerprint = self._fingerprint(raw)
        expected = self._token(client_id, server_id, fingerprint)
        if not hmac.compare_digest(preview_token, expected):
            raise McpInspectorConflict("配置已变化或预览令牌无效，请重新预览。")
        if record["transport"] != "stdio":
            raise McpInspectorError("当前仅允许对本地 stdio MCP 做单次探测。")
        try:
            result = await self._probe_runner(raw, timeout_seconds)
        except TimeoutError as exc:
            raise McpInspectorError("MCP 探测超时，临时进程已进入回收流程。") from exc
        except McpInspectorError:
            raise
        except Exception as exc:
            raise McpInspectorError(f"MCP 探测失败：{type(exc).__name__}") from exc
        safe = {
            "status": "passed",
            "server_id": server_id,
            "client_id": client_id,
            "transport": "stdio",
            "server_name": str(result.get("server_name", "MCP Server"))[:200],
            "server_version": str(result.get("server_version", "unknown"))[:100],
            "protocol_version": str(result.get("protocol_version", "unknown"))[:100],
            "tools": result.get("tools", []),
            "resources": result.get("resources", []),
            "prompts": result.get("prompts", []),
            "tool_count": len(result.get("tools", [])),
            "resource_count": len(result.get("resources", [])),
            "prompt_count": len(result.get("prompts", [])),
            "process_reclaimed": True,
            "secret_values_exposed": False,
        }
        self._latest[server_id] = safe
        return safe

    def _resolve(self, client_id: str, server_id: str) -> dict[str, Any]:
        for name, raw in self._load_servers(client_id).items():
            if _stable_id(client_id, name) != server_id:
                continue
            transport = "streamable-http" if isinstance(raw.get("url"), str) else "stdio"
            return {
                "id": server_id,
                "client_id": client_id,
                "name": name,
                "transport": transport,
                "enabled": self._enabled(client_id, raw),
                "config_keys": sorted(str(key) for key in raw),
                "argument_count": (
                    len(raw.get("args", [])) if isinstance(raw.get("args"), list) else 0
                ),
                "environment_variable_count": (
                    len(raw.get("env", {})) if isinstance(raw.get("env"), dict) else 0
                ),
                "raw": raw,
            }
        raise McpInspectorError("未找到该 MCP 配置。")

    def _load_servers(self, client_id: str) -> dict[str, dict[str, Any]]:
        if client_id == "codex":
            path = self.home / ".codex" / "config.toml"
            try:
                parsed = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
                raise McpInspectorError("Codex MCP 配置不可读取。") from exc
            raw = parsed.get("mcp_servers", {})
        else:
            candidates = {
                "cursor": self.home / ".cursor" / "mcp.json",
                "claude-desktop": (
                    self.home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
                ),
            }
            path = candidates.get(client_id)
            if path is None:
                raise McpInspectorError("不支持该客户端。")
            try:
                parsed = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise McpInspectorError("客户端 MCP 配置不可读取。") from exc
            raw = parsed.get("mcpServers", {})
        if not isinstance(raw, dict):
            return {}
        return {str(name): value for name, value in raw.items() if isinstance(value, dict)}

    @staticmethod
    def _enabled(client_id: str, raw: dict[str, Any]) -> bool:
        if client_id == "codex":
            return raw.get("enabled", True) is not False
        return raw.get("disabled", False) is not True

    @staticmethod
    def _fingerprint(raw: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()

    def _token(self, client_id: str, server_id: str, fingerprint: str) -> str:
        payload = f"{client_id}\x1f{server_id}\x1f{fingerprint}".encode()
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()

    @staticmethod
    async def _probe_stdio(raw: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpInspectorError("stdio MCP 缺少可执行命令。")
        args = raw.get("args", [])
        env = raw.get("env")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise McpInspectorError("MCP 参数格式无效。")
        valid_env = env is None or (
            isinstance(env, dict)
            and all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())
        )
        if not valid_env:
            raise McpInspectorError("MCP 环境变量格式无效。")
        stderr = io.StringIO()
        with anyio.fail_after(timeout_seconds):
            parameters = StdioServerParameters(command=command, args=args, env=env)
            async with (
                stdio_client(parameters, errlog=stderr) as streams,
                ClientSession(*streams) as session,
            ):
                initialized = await session.initialize()
                tool_items = (await session.list_tools()).tools
                tools = [
                    {
                        "name": tool.name,
                        "description": (tool.description or "")[:500],
                        "input_schema": tool.input_schema,
                    }
                    for tool in tool_items
                ]
                resources: list[dict[str, Any]] = []
                prompts: list[dict[str, Any]] = []
                if initialized.capabilities.resources is not None:
                    resource_items = (await session.list_resources()).resources
                    resources = [
                        {"name": item.name, "uri": str(item.uri)} for item in resource_items
                    ]
                if initialized.capabilities.prompts is not None:
                    prompt_items = (await session.list_prompts()).prompts
                    prompts = [
                        {"name": item.name, "description": (item.description or "")[:500]}
                        for item in prompt_items
                    ]
                return {
                    "server_name": initialized.server_info.name,
                    "server_version": initialized.server_info.version,
                    "protocol_version": initialized.protocol_version,
                    "tools": tools,
                    "resources": resources,
                    "prompts": prompts,
                }
