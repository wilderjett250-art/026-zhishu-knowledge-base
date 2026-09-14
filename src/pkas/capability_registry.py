from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


def _stable_id(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _frontmatter(skill_file: Path) -> dict[str, str]:
    """Read only simple scalar metadata; skill instructions are not returned by the API."""
    try:
        lines = skill_file.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            result[key.strip()] = value.strip().strip("'\"")
    return result


def _directory_fingerprint(root: Path) -> tuple[str, int, list[str]]:
    """Return a stable, content-based fingerprint without exposing absolute paths."""
    digest = hashlib.sha256()
    resources: list[str] = []
    total_bytes = 0
    try:
        files = sorted(item for item in root.rglob("*") if item.is_file())
    except OSError:
        return "unavailable", 0, []
    for item in files:
        try:
            relative = item.relative_to(root).as_posix()
            payload = item.read_bytes()
        except OSError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        total_bytes += len(payload)
        if relative != "SKILL.md":
            resources.append(relative)
    return digest.hexdigest(), total_bytes, resources


@dataclass(frozen=True, slots=True)
class ClientSnapshot:
    client: dict[str, Any]
    skills: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]
    warnings: list[str]


class ClientAdapter(Protocol):
    client_id: str

    def inspect(self, home: Path) -> ClientSnapshot: ...


class CodexClientAdapter:
    """Secret-safe, read-only discovery for the first supported AI client."""

    client_id = "codex"

    def __init__(self, *, audit_resources: bool = True) -> None:
        self.audit_resources = audit_resources

    def inspect(self, home: Path) -> ClientSnapshot:
        codex_root = home / ".codex"
        config_path = codex_root / "config.toml"
        skill_root = codex_root / "skills"
        warnings: list[str] = []
        config: dict[str, Any] = {}
        if config_path.is_file():
            try:
                config = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                warnings.append("Codex 配置存在但无法解析；未读取或返回任何配置值。")

        skills = self._skills(skill_root)
        mcp_servers = self._mcp_servers(config)
        detected = codex_root.is_dir() or config_path.is_file()
        return ClientSnapshot(
            client={
                "id": self.client_id,
                "name": "Codex",
                "kind": "coding-agent",
                "detected": detected,
                "status": "detected" if detected else "not_detected",
                "config_hint": r"%USERPROFILE%\.codex\config.toml",
                "adapter_version": "codex-readonly-v1",
                "management_mode": "transactional",
                "skill_count": len(skills),
                "mcp_server_count": len(mcp_servers),
            },
            skills=skills,
            mcp_servers=mcp_servers,
            warnings=warnings,
        )

    def _skills(self, root: Path) -> list[dict[str, Any]]:
        if not root.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for skill_file in sorted(root.glob("*/SKILL.md")):
            metadata = _frontmatter(skill_file)
            skill_dir = skill_file.parent
            scope = "system" if skill_dir.name.startswith(".") else "personal"
            content_hash, total_bytes, resources = (
                _directory_fingerprint(skill_dir)
                if self.audit_resources
                else ("not_checked", 0, [])
            )
            records.append(
                {
                    "id": _stable_id(self.client_id, str(skill_dir.resolve())),
                    "client_id": self.client_id,
                    "name": metadata.get("name") or skill_dir.name,
                    "description": metadata.get("description", ""),
                    "scope": scope,
                    "status": "available",
                    "path_hint": rf"%USERPROFILE%\.codex\skills\{skill_dir.name}",
                    "resource_count": len(resources),
                    "resources": resources[:100],
                    "resources_truncated": len(resources) > 100,
                    "content_hash": content_hash,
                    "content_hash_short": content_hash[:12],
                    "total_bytes": total_bytes,
                    "manifest": "SKILL.md",
                    "validation": (
                        "not_checked"
                        if not self.audit_resources
                        else "valid"
                        if content_hash != "unavailable"
                        else "unreadable"
                    ),
                    "editable": scope == "personal",
                }
            )
        return records

    def _mcp_servers(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        raw_servers = config.get("mcp_servers")
        if not isinstance(raw_servers, dict):
            return []
        records: list[dict[str, Any]] = []
        for name, raw in sorted(raw_servers.items()):
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            transport = "streamable-http" if isinstance(raw.get("url"), str) else "stdio"
            enabled = raw.get("enabled", True) is not False
            config_shape = sorted(str(key) for key in raw)
            records.append(
                {
                    "id": _stable_id(self.client_id, name),
                    "client_id": self.client_id,
                    "name": name,
                    "transport": transport,
                    "enabled": enabled,
                    "status": "configured" if enabled else "paused",
                    "scope": "user",
                    "config_keys": config_shape,
                    "argument_count": (
                        len(raw.get("args", [])) if isinstance(raw.get("args"), list) else 0
                    ),
                    "environment_variable_count": (
                        len(raw.get("env", {})) if isinstance(raw.get("env"), dict) else 0
                    ),
                    "probe_supported": transport == "stdio",
                    "secret_values_exposed": False,
                }
            )
        return records


class JsonMcpClientAdapter:
    """Secret-safe discovery for clients that store MCP servers in JSON."""

    client_id = "json-client"
    name = "JSON MCP Client"
    kind = "ai-client"
    relative_candidates: tuple[str, ...] = ()

    def inspect(self, home: Path) -> ClientSnapshot:
        config_path = self._config_path(home)
        warnings: list[str] = []
        config: dict[str, Any] = {}
        if config_path.is_file():
            try:
                parsed = json.loads(config_path.read_text(encoding="utf-8-sig"))
                if isinstance(parsed, dict):
                    config = parsed
                else:
                    warnings.append(f"{self.name} 配置顶层不是对象；未返回配置值。")
            except (OSError, UnicodeError, json.JSONDecodeError):
                warnings.append(f"{self.name} 配置存在但无法解析；未返回配置值。")
        raw_servers = config.get("mcpServers")
        servers = raw_servers if isinstance(raw_servers, dict) else {}
        records: list[dict[str, Any]] = []
        for server_name, raw in sorted(servers.items()):
            if not isinstance(server_name, str) or not isinstance(raw, dict):
                continue
            transport = "streamable-http" if isinstance(raw.get("url"), str) else "stdio"
            enabled = raw.get("disabled", False) is not True
            config_shape = sorted(str(key) for key in raw)
            records.append(
                {
                    "id": _stable_id(self.client_id, server_name),
                    "client_id": self.client_id,
                    "name": server_name,
                    "transport": transport,
                    "enabled": enabled,
                    "status": "configured" if enabled else "paused",
                    "scope": "user",
                    "config_keys": config_shape,
                    "argument_count": (
                        len(raw.get("args", [])) if isinstance(raw.get("args"), list) else 0
                    ),
                    "environment_variable_count": (
                        len(raw.get("env", {})) if isinstance(raw.get("env"), dict) else 0
                    ),
                    "probe_supported": transport == "stdio",
                    "secret_values_exposed": False,
                }
            )
        detected = config_path.is_file() or config_path.parent.is_dir()
        return ClientSnapshot(
            client={
                "id": self.client_id,
                "name": self.name,
                "kind": self.kind,
                "detected": detected,
                "status": "detected" if detected else "not_detected",
                "config_hint": self._path_hint(config_path, home),
                "adapter_version": f"{self.client_id}-readonly-v1",
                "management_mode": "transactional",
                "skill_count": 0,
                "mcp_server_count": len(records),
            },
            skills=[],
            mcp_servers=records,
            warnings=warnings,
        )

    def _config_path(self, home: Path) -> Path:
        candidates = [home / Path(value) for value in self.relative_candidates]
        for path in candidates:
            if path.is_file():
                return path
        if self.client_id == "claude-desktop" and sys.platform == "darwin":
            return candidates[1]
        return candidates[0]

    def _path_hint(self, path: Path, home: Path) -> str:
        try:
            return str(Path(r"%USERPROFILE%") / path.relative_to(home))
        except ValueError:
            return path.name


class ClaudeDesktopClientAdapter(JsonMcpClientAdapter):
    client_id = "claude-desktop"
    name = "Claude Desktop"
    kind = "desktop-assistant"
    relative_candidates = (
        "AppData/Roaming/Claude/claude_desktop_config.json",
        "Library/Application Support/Claude/claude_desktop_config.json",
    )


class CursorClientAdapter(JsonMcpClientAdapter):
    client_id = "cursor"
    name = "Cursor"
    kind = "coding-agent"
    relative_candidates = (".cursor/mcp.json",)


class CapabilityRegistry:
    """Aggregates client adapters without coupling the knowledge core to any client."""

    schema_version = "pkas.capability-overview.v1"

    def __init__(self, home: Path, adapters: list[ClientAdapter] | None = None) -> None:
        self.home = home
        self.adapters = adapters or [
            CodexClientAdapter(),
            ClaudeDesktopClientAdapter(),
            CursorClientAdapter(),
        ]

    def overview(
        self,
        *,
        knowledge_stats: dict[str, Any],
        rag_status: dict[str, Any],
    ) -> dict[str, Any]:
        snapshots = [adapter.inspect(self.home) for adapter in self.adapters]
        clients = [snapshot.client for snapshot in snapshots]
        skills = [item for snapshot in snapshots for item in snapshot.skills]
        mcp_servers = [item for snapshot in snapshots for item in snapshot.mcp_servers]
        warnings = [warning for snapshot in snapshots for warning in snapshot.warnings]
        counts = knowledge_stats.get("counts", {})
        qdrant = rag_status.get("qdrant", {})
        interfaces = [
            {
                "id": "rest-api",
                "name": "REST API",
                "status": "ready",
                "route": "/api",
                "audience": "web-sdk-automation",
            },
            {
                "id": "mcp-provider",
                "name": "MCP Provider",
                "status": "paused" if not rag_status.get("mcp_enabled") else "ready",
                "route": "stdio",
                "audience": "mcp-clients",
            },
            {
                "id": "openai-compatible",
                "name": "OpenAI-compatible Gateway",
                "status": "planned",
                "route": "/v1",
                "audience": "api-compatible-clients",
            },
        ]
        services = [
            {
                "id": "knowledge-core",
                "name": "知识与全文索引",
                "status": "ready",
                "engine": "SQLite + FTS5",
                "records": int(counts.get("knowledge_chunks", 0)),
            },
            {
                "id": "vector-index",
                "name": "向量索引",
                "status": qdrant.get("status", "unknown"),
                "engine": "Qdrant",
                "records": int(qdrant.get("points", 0) or 0),
            },
            {
                "id": "agent-runtime",
                "name": "Agent Runtime",
                "status": "available",
                "engine": "LangGraph + DeepSeek",
                "records": int(counts.get("agent_runs", 0)),
            },
            {
                "id": "workflow-engine",
                "name": "工作流引擎",
                "status": "available",
                "engine": "PKAS deterministic workflows",
                "records": int(counts.get("workflow_runs", 0)),
            },
        ]
        return {
            "schema_version": self.schema_version,
            "platform": {
                "name": "PKAS AI Control Plane",
                "architecture": "client-neutral-modular-monolith",
                "management_mode": "transactional_adapters",
            },
            "summary": {
                "clients": len(clients),
                "detected_clients": sum(1 for item in clients if item["detected"]),
                "skills": len(skills),
                "mcp_servers": len(mcp_servers),
                "enabled_mcp_servers": sum(1 for item in mcp_servers if item["enabled"]),
                "profiles": int(counts.get("profiles", 0)),
            },
            "clients": clients,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "interfaces": interfaces,
            "services": services,
            "warnings": warnings,
            "safety": {
                "configuration_writes_enabled": True,
                "secret_values_returned": False,
                "backup_required_before_writes": True,
                "preview_required_before_writes": True,
                "automatic_rollback_on_failure": True,
            },
        }
