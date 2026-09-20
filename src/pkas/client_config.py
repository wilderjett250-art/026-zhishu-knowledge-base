from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import os
import stat
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomlkit
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pkas.config import Settings
from pkas.local_secrets import protect_user_bytes, unprotect_user_bytes


class ClientConfigError(RuntimeError):
    pass


class ClientConfigConflict(ClientConfigError):
    pass


class ClientConnectionError(ClientConfigError):
    pass


@dataclass(frozen=True, slots=True)
class ClientConfigSpec:
    client_id: str
    name: str
    format: str
    relative_candidates: tuple[str, ...]


CLIENT_SPECS = {
    "codex": ClientConfigSpec(
        client_id="codex",
        name="Codex",
        format="toml",
        relative_candidates=(".codex/config.toml",),
    ),
    "claude-desktop": ClientConfigSpec(
        client_id="claude-desktop",
        name="Claude Desktop",
        format="json",
        relative_candidates=(
            "AppData/Roaming/Claude/claude_desktop_config.json",
            "Library/Application Support/Claude/claude_desktop_config.json",
            ".config/Claude/claude_desktop_config.json",
        ),
    ),
    "cursor": ClientConfigSpec(
        client_id="cursor",
        name="Cursor",
        format="json",
        relative_candidates=(".cursor/mcp.json",),
    ),
}


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _redact(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in ("token", "secret", "password", "cookie", "api_key")):
        return "<redacted>"
    if lowered == "env" and isinstance(value, dict):
        return {str(item): "<redacted>" for item in value}
    if isinstance(value, dict):
        return {str(item): _redact(child, str(item)) for item, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, key) for child in value]
    return value


class ClientConfigTransactionService:
    schema_version = "pkas.client-config-transaction.v1"

    def __init__(
        self,
        settings: Settings,
        home: Path,
        *,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.settings = settings
        self.home = home.resolve()
        self._protect = protect or (
            lambda value: protect_user_bytes(value, description="PKAS client config rollback")
        )
        self._unprotect = unprotect or unprotect_user_bytes

    def client_statuses(self) -> list[dict[str, Any]]:
        return [self.inspect(client_id) for client_id in CLIENT_SPECS]

    def inspect(self, client_id: str) -> dict[str, Any]:
        spec = self._spec(client_id)
        path = self._config_path(spec)
        raw = path.read_bytes() if path.is_file() else b""
        configured = False
        enabled = False
        valid = True
        warning = ""
        if raw:
            try:
                document = self._parse(spec, raw)
                configured, enabled = self._pkas_state(spec, document)
            except ClientConfigError as exc:
                valid = False
                warning = str(exc)
        return {
            "id": spec.client_id,
            "name": spec.name,
            "detected": path.is_file() or path.parent.is_dir(),
            "config_exists": path.is_file(),
            "config_valid": valid,
            "pkas_configured": configured,
            "pkas_enabled": enabled,
            "management_mode": "transactional",
            "config_hint": self._path_hint(path),
            "warning": warning,
        }

    def preview(self, client_id: str, *, enabled: bool) -> dict[str, Any]:
        spec = self._spec(client_id)
        path = self._config_path(spec)
        before = path.read_bytes() if path.is_file() else b""
        after = self._render(spec, before, enabled=enabled)
        before_hash = _hash(before)
        after_hash = _hash(after)
        token = _hash(
            "\x1f".join(
                (spec.client_id, str(path), before_hash, after_hash, str(enabled))
            ).encode("utf-8")
        )
        return {
            "schema_version": self.schema_version,
            "client_id": spec.client_id,
            "client_name": spec.name,
            "desired_enabled": enabled,
            "config_hint": self._path_hint(path),
            "config_exists": path.is_file(),
            "will_create": not path.is_file(),
            "before_hash": before_hash,
            "after_hash": after_hash,
            "changed": before != after,
            "redacted_diff": self._redacted_diff(spec, before, after),
            "preview_token": token,
            "secret_values_returned": False,
            "rollback_backup": "current-user protected",
            "connection_test": "MCP initialize + list_tools" if enabled else "not required",
        }

    async def apply(
        self,
        client_id: str,
        *,
        enabled: bool,
        preview_token: str,
    ) -> dict[str, Any]:
        preview = self.preview(client_id, enabled=enabled)
        if preview["preview_token"] != preview_token:
            raise ClientConfigConflict("客户端配置在预览后发生变化，请重新预览。")
        spec = self._spec(client_id)
        path = self._config_path(spec)
        before = path.read_bytes() if path.is_file() else b""
        after = self._render(spec, before, enabled=enabled)
        backup_id = self._save_backup(spec, path, before, existed=path.is_file())
        connection: dict[str, Any] = {"status": "not_required", "tools": 0}
        try:
            self._atomic_write(path, after)
            self._parse(spec, path.read_bytes())
            if enabled:
                connection = await self._probe_mcp()
        except Exception as exc:
            self._restore_bytes(path, before, existed=bool(before) or preview["config_exists"])
            self._record_audit(
                spec,
                status="rolled_back",
                before_hash=_hash(before),
                after_hash=_hash(after),
                backup_id=backup_id,
                detail=type(exc).__name__,
            )
            if isinstance(exc, ClientConfigError):
                raise
            raise ClientConfigError("客户端配置验证失败，已自动恢复原配置。") from exc
        self._record_audit(
            spec,
            status="applied",
            before_hash=_hash(before),
            after_hash=_hash(after),
            backup_id=backup_id,
            detail=connection["status"],
        )
        return {
            "client_id": spec.client_id,
            "status": "applied",
            "enabled": enabled,
            "config_hint": self._path_hint(path),
            "backup_id": backup_id,
            "connection": connection,
            "secret_values_returned": False,
        }

    def list_backups(self, client_id: str) -> list[dict[str, Any]]:
        spec = self._spec(client_id)
        root = self._backup_root(spec)
        if not root.is_dir():
            return []
        return [
            {"backup_id": item.name, "client_id": client_id, "protected": True}
            for item in sorted(root.glob("*.dpapi"), reverse=True)
        ]

    def rollback(self, client_id: str, backup_id: str) -> dict[str, Any]:
        spec = self._spec(client_id)
        if Path(backup_id).name != backup_id or not backup_id.endswith(".dpapi"):
            raise ClientConfigError("备份标识不合法。")
        backup_path = self._backup_root(spec) / backup_id
        if not backup_path.is_file():
            raise ClientConfigError("客户端配置备份不存在。")
        try:
            envelope = json.loads(self._unprotect(backup_path.read_bytes()).decode("utf-8"))
            if envelope.get("client_id") != spec.client_id:
                raise ClientConfigError("客户端配置备份不匹配。")
            path = self._config_path(spec)
            if envelope.get("path") != str(path):
                raise ClientConfigError("客户端配置路径与备份不匹配。")
            existed = bool(envelope.get("existed"))
            raw = base64.b64decode(envelope.get("content", "")) if existed else b""
            if existed:
                self._parse(spec, raw)
            current = path.read_bytes() if path.is_file() else b""
            self._restore_bytes(path, raw, existed=existed)
        except ClientConfigError:
            raise
        except Exception as exc:
            raise ClientConfigError("客户端配置备份无法恢复。") from exc
        self._record_audit(
            spec,
            status="manual_rollback",
            before_hash=_hash(current),
            after_hash=_hash(raw),
            backup_id=backup_id,
            detail="restored",
        )
        return {
            "client_id": client_id,
            "status": "restored",
            "config_hint": self._path_hint(path),
            "secret_values_returned": False,
        }

    async def _probe_mcp(self) -> dict[str, Any]:
        command, args = self._mcp_command()
        params = StdioServerParameters(
            command=command,
            args=args,
            cwd=str(self.settings.project_root),
            env=self._mcp_environment(),
        )
        try:
            async with asyncio.timeout(20):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        return {
                            "status": "passed",
                            "server": initialized.server_info.name,
                            "tools": len(tools.tools),
                        }
        except Exception as exc:
            raise ClientConnectionError("PKAS MCP 真实握手失败，客户端配置已回滚。") from exc

    def _spec(self, client_id: str) -> ClientConfigSpec:
        try:
            return CLIENT_SPECS[client_id]
        except KeyError as exc:
            raise ClientConfigError("不支持的客户端。") from exc

    def _config_path(self, spec: ClientConfigSpec) -> Path:
        candidates = [self.home / Path(value) for value in spec.relative_candidates]
        for path in candidates:
            if path.is_file():
                return path
        if spec.client_id == "claude-desktop" and sys.platform == "darwin":
            return candidates[1]
        if spec.client_id == "claude-desktop" and os.name != "nt":
            return candidates[2]
        return candidates[0]

    def _mcp_command(self) -> tuple[str, list[str]]:
        if os.name == "nt":
            packaged = Path(sys.executable).with_name("python.exe")
            candidates = (
                self.settings.project_root / ".venv" / "Scripts" / "python.exe",
                packaged,
            )
        else:
            candidates = (
                self.settings.project_root / ".venv" / "bin" / "python",
                Path(sys.executable),
            )
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
        if executable is None:
            raise ClientConfigError("PKAS MCP Python运行时不存在，不能生成客户端配置。")
        return str(executable), ["-m", "pkas.mcp_server"]

    def _mcp_environment(self) -> dict[str, str]:
        return {
            "PKAS_PROJECT_ROOT": str(self.settings.project_root),
            "PKAS_DATA_ROOT": str(self.settings.data_root),
            "PKAS_ENV_FILE": str(self.settings.data_root / "config" / ".env"),
            "PKAS_QDRANT_URL": self.settings.qdrant_url,
        }

    def _parse(self, spec: ClientConfigSpec, raw: bytes) -> Any:
        try:
            text = raw.decode("utf-8-sig")
            if spec.format == "toml":
                return tomlkit.parse(text)
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ClientConfigError("客户端配置顶层必须是对象。")
            return value
        except ClientConfigError:
            raise
        except (UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ClientConfigError(f"{spec.name} 配置无法解析，未执行写入。") from exc
        except Exception as exc:
            raise ClientConfigError(f"{spec.name} 配置无法解析，未执行写入。") from exc

    def _render(self, spec: ClientConfigSpec, before: bytes, *, enabled: bool) -> bytes:
        command, args = self._mcp_command()
        if spec.format == "toml":
            document = self._parse(spec, before) if before else tomlkit.document()
            servers = document.get("mcp_servers")
            if servers is None:
                servers = tomlkit.table()
                document["mcp_servers"] = servers
            if not hasattr(servers, "get"):
                raise ClientConfigError("Codex mcp_servers 配置结构无效。")
            entry = servers.get("personal_knowledge") or tomlkit.table()
            entry["command"] = command
            entry["args"] = args
            existing_env = entry.get("env", {})
            safe_existing_env = dict(existing_env) if hasattr(existing_env, "items") else {}
            entry["env"] = {**safe_existing_env, **self._mcp_environment()}
            entry["enabled"] = enabled
            servers["personal_knowledge"] = entry
            rendered = tomlkit.dumps(document)
            tomllib.loads(rendered)
            return rendered.encode("utf-8")
        document = self._parse(spec, before) if before else {}
        servers = document.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ClientConfigError(f"{spec.name} mcpServers 配置结构无效。")
        if enabled:
            entry = servers.get("personal_knowledge")
            if not isinstance(entry, dict):
                entry = {}
            existing_env = entry.get("env")
            safe_existing_env = existing_env if isinstance(existing_env, dict) else {}
            entry.update(
                {
                    "command": command,
                    "args": args,
                    "env": {**safe_existing_env, **self._mcp_environment()},
                }
            )
            servers["personal_knowledge"] = entry
        else:
            servers.pop("personal_knowledge", None)
        return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def _pkas_state(self, spec: ClientConfigSpec, document: Any) -> tuple[bool, bool]:
        key = "mcp_servers" if spec.format == "toml" else "mcpServers"
        servers = document.get(key, {}) if hasattr(document, "get") else {}
        if not hasattr(servers, "get"):
            return False, False
        entry = servers.get("personal_knowledge")
        if entry is None:
            return False, False
        enabled = entry.get("enabled", True) is not False if hasattr(entry, "get") else True
        return True, enabled

    def _redacted_diff(self, spec: ClientConfigSpec, before: bytes, after: bytes) -> list[str]:
        def safe_lines(raw: bytes) -> list[str]:
            if not raw:
                return []
            parsed = self._parse(spec, raw)
            if spec.format == "toml":
                parsed = tomllib.loads(raw.decode("utf-8-sig"))
            return json.dumps(_redact(parsed), ensure_ascii=False, indent=2).splitlines()

        return list(
            difflib.unified_diff(
                safe_lines(before),
                safe_lines(after),
                fromfile="current-redacted",
                tofile="proposed-redacted",
                lineterm="",
            )
        )[:300]

    def _backup_root(self, spec: ClientConfigSpec) -> Path:
        return self.settings.data_root / "secrets" / "client-config-backups" / spec.client_id

    def _save_backup(
        self,
        spec: ClientConfigSpec,
        path: Path,
        before: bytes,
        *,
        existed: bool,
    ) -> str:
        envelope = json.dumps(
            {
                "schema_version": 1,
                "client_id": spec.client_id,
                "path": str(path),
                "existed": existed,
                "content": base64.b64encode(before).decode("ascii") if existed else "",
                "content_hash": _hash(before),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        root = self._backup_root(spec)
        root.mkdir(parents=True, exist_ok=True)
        backup_id = f"{_utc_stamp()}-{_hash(before)[:12]}.dpapi"
        target = root / backup_id
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(self._protect(envelope))
        temporary.replace(target)
        for stale in sorted(root.glob("*.dpapi"), reverse=True)[3:]:
            stale.unlink(missing_ok=True)
        return backup_id

    def _atomic_write(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        old_mode = stat.S_IMODE(path.stat().st_mode) if path.is_file() else None
        temporary = path.with_name(f".{path.name}.pkas-{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if old_mode is not None:
                os.chmod(temporary, old_mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_bytes(self, path: Path, content: bytes, *, existed: bool) -> None:
        if existed:
            self._atomic_write(path, content)
        else:
            path.unlink(missing_ok=True)

    def _record_audit(
        self,
        spec: ClientConfigSpec,
        *,
        status: str,
        before_hash: str,
        after_hash: str,
        backup_id: str,
        detail: str,
    ) -> None:
        root = self.settings.data_root / "runs" / "client-config"
        root.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": self.schema_version,
            "at": datetime.now(UTC).isoformat(),
            "client_id": spec.client_id,
            "status": status,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "backup_id": backup_id,
            "detail": detail,
            "secret_values_recorded": False,
        }
        with (root / "transactions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _path_hint(self, path: Path) -> str:
        for root, marker in (
            (self.home, "%USERPROFILE%"),
            (self.settings.project_root.resolve(), "%PKAS_ROOT%"),
        ):
            try:
                relative = path.resolve().relative_to(root)
            except ValueError:
                continue
            return str(Path(marker) / relative)
        return path.name
