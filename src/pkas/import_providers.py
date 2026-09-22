"""Safe, explicit source-adapter registry for the desktop import surface.

This module deliberately does *not* load arbitrary code, run third-party
executables, extract credentials, or read protected application databases.
Adapters describe the import formats that the product understands and route
users to a read-only inspection or an explicit import confirmation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pkas.config import Settings
from pkas.sync import SyncService
from pkas.weflow import WeFlowService


class ImportProviderError(ValueError):
    """Raised for an invalid provider request without exposing local details."""


class ImportProviderNotFound(ImportProviderError):
    """Raised when an API caller requests an unknown built-in provider."""


@dataclass(frozen=True, slots=True)
class ImportProviderManifest:
    provider_id: str
    title: str
    summary: str
    category: str
    delivery: str
    formats: tuple[str, ...]
    privacy_boundary: str
    actions: tuple[str, ...]
    requires_external_app: bool = False
    supports_background_sync: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "title": self.title,
            "summary": self.summary,
            "category": self.category,
            "delivery": self.delivery,
            "formats": list(self.formats),
            "privacy_boundary": self.privacy_boundary,
            "actions": list(self.actions),
            "requires_external_app": self.requires_external_app,
            "supports_background_sync": self.supports_background_sync,
        }


class ImportProvider(Protocol):
    manifest: ImportProviderManifest

    def probe(
        self,
        *,
        location: str | None = None,
        records_path: str | None = None,
        weflow_root: str | None = None,
    ) -> dict[str, Any]: ...


def _existing_path(value: str | None) -> tuple[Path | None, str]:
    """Resolve only user-supplied path metadata without reading its contents."""
    raw = str(value or "").strip()
    if not raw:
        return None, "not_configured"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None, "invalid_path"
    try:
        return candidate.resolve(strict=True), "available"
    except (OSError, RuntimeError):
        return None, "not_found"


class LocalFilesProvider:
    manifest = ImportProviderManifest(
        provider_id="local-files",
        title="本地文件和文件夹",
        summary="导入你明确选择的文件或目录；先检查范围，再确认入库。",
        category="files",
        delivery="built_in",
        formats=("Word", "Excel", "PDF", "PPT", "Markdown", "文本", "代码"),
        privacy_boundary="不会自动扫盘；原件保持在原处，确认后才建立资料记录。",
        actions=("/api/import/inspect", "/api/import/run"),
    )

    def probe(
        self,
        *,
        location: str | None = None,
        records_path: str | None = None,
        weflow_root: str | None = None,
    ) -> dict[str, Any]:
        path, path_state = _existing_path(location)
        return {
            "provider": self.manifest.as_dict(),
            "status": "ready" if path_state in {"not_configured", "available"} else path_state,
            "location_kind": "directory_or_file" if path and path.exists() else None,
            "content_read": False,
            "next_step": "选择一个文件或文件夹后执行只读范围检查。",
        }


class ObsidianVaultProvider:
    manifest = ImportProviderManifest(
        provider_id="obsidian-vault",
        title="Obsidian 笔记库",
        summary="将一个 Obsidian Vault 作为独立资料源接入，忽略应用配置与回收站。",
        category="notes",
        delivery="built_in",
        formats=("Markdown", "YAML frontmatter", "Obsidian attachments"),
        privacy_boundary="探测阶段只看目录结构；正文仍需通过后续确认才会读取或索引。",
        actions=("/api/import-providers/obsidian-vault/probe", "register_sync_root"),
    )

    def __init__(self, sync: SyncService) -> None:
        self.sync = sync

    def probe(
        self,
        *,
        location: str | None = None,
        records_path: str | None = None,
        weflow_root: str | None = None,
    ) -> dict[str, Any]:
        vault, path_state = _existing_path(location)
        base: dict[str, Any] = {
            "provider": self.manifest.as_dict(),
            "content_read": False,
            "vault_detected": False,
            "configuration_directory_present": False,
            "top_level_markdown_files": 0,
            "top_level_folders": 0,
        }
        if path_state == "not_configured":
            return {
                **base,
                "status": "needs_vault_path",
                "next_step": "选择你的 Obsidian Vault 根目录；不会自动读取笔记正文。",
            }
        if path_state != "available" or vault is None:
            return {
                **base,
                "status": path_state,
                "next_step": "确认该 Vault 根目录存在且使用绝对路径。",
            }
        if not vault.is_dir():
            return {
                **base,
                "status": "not_a_directory",
                "next_step": "请选择 Obsidian Vault 的文件夹，而不是单个文件。",
            }
        try:
            children = list(vault.iterdir())
        except OSError:
            return {
                **base,
                "status": "unreadable",
                "next_step": "当前 Windows 用户无法读取该目录的基础元数据。",
            }
        has_configuration = (vault / ".obsidian").is_dir()
        return {
            **base,
            "status": "ready" if has_configuration else "not_obsidian_vault",
            "vault_detected": has_configuration,
            "configuration_directory_present": has_configuration,
            "top_level_markdown_files": sum(
                1 for item in children if item.is_file() and item.suffix.lower() == ".md"
            ),
            "top_level_folders": sum(1 for item in children if item.is_dir()),
            "next_step": (
                "已识别 Vault；确认登记后可选择只建目录、全文或语义处理。"
                if has_configuration
                else "该目录没有 .obsidian 配置目录；可作为普通资料目录接入。"
            ),
        }

    def register(
        self,
        *,
        vault_path: str,
        name: str,
        domain: str,
        privacy: str,
        sync_mode: str,
        recursive: bool,
    ) -> dict[str, Any]:
        probe = self.probe(location=vault_path)
        if probe["status"] != "ready":
            raise ImportProviderError("尚未识别到有效的 Obsidian Vault，未登记任何资料源。")
        root = self.sync.register_root(
            name=name.strip() or "Obsidian 笔记库",
            root_path=vault_path,
            connector_type="obsidian_vault",
            domain=domain,
            privacy=privacy,
            sync_mode=sync_mode,
            recursive=recursive,
            config={
                "provider_id": self.manifest.provider_id,
                "excluded_directories": [".obsidian", ".trash", ".git"],
                "registered_as": "obsidian_vault",
            },
        )
        return {
            "provider": self.manifest.as_dict(),
            "root": root,
            "content_read": False,
            "scan_started": False,
            "next_step": "资料源已登记；请在资料管理中主动执行首次扫描。",
        }


class WeFlowLegacyExportProvider:
    manifest = ImportProviderManifest(
        provider_id="weflow-legacy-export",
        title="WeFlow 已导出聊天文件",
        summary="兼容已存在的 WeFlow XLSX 导出；知域只读取你勾选的导出文件。",
        category="chat_export",
        delivery="bring_your_own_exporter",
        formats=("WeFlow XLSX", "export-record index"),
        privacy_boundary="不读取微信密钥、不直接访问 WCDB，不随安装包提供第三方导出器。",
        actions=(
            "/api/weflow/exports/discover",
            "/api/weflow/exports/inspect",
            "/api/weflow/exports/import",
        ),
        requires_external_app=True,
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def probe(
        self,
        *,
        location: str | None = None,
        records_path: str | None = None,
        weflow_root: str | None = None,
    ) -> dict[str, Any]:
        records, records_state = _existing_path(
            records_path or str(self.settings.weflow_export_records_path)
        )
        root, root_state = _existing_path(weflow_root)
        runtime_present = False
        if root_state == "available" and root is not None and root.is_dir():
            runtime_present = all(
                item.is_file()
                for item in (
                    root / "package.json",
                    root / "node_modules" / "electron" / "dist" / "electron.exe",
                )
            )
        records_ready = records_state == "available" and records is not None and records.is_file()
        return {
            "provider": self.manifest.as_dict(),
            "status": "existing_exports_ready" if records_ready else "needs_user_export",
            "content_read": False,
            "key_accessed": False,
            "database_accessed": False,
            "export_index_present": records_ready,
            "legacy_runtime_detected": runtime_present,
            "manual_sync_eligible": bool(records_ready and runtime_present and os.name == "nt"),
            "next_step": (
                "可发现并勾选已有 XLSX；导入前仍会单独做只读结构检查。"
                if records_ready
                else "请先使用你自行取得且已登录的导出工具生成聊天文件，再由知域导入。"
            ),
        }


class ChatLabFileProvider:
    manifest = ImportProviderManifest(
        provider_id="chatlab-file",
        title="ChatLab 兼容聊天文件",
        summary="导入符合知域 ChatLab 兼容约定的微信会话 JSON；不限于某一个导出工具。",
        category="chat_export",
        delivery="built_in",
        formats=("ChatLab-compatible WeChat JSON v1",),
        privacy_boundary="先检查文件结构和哈希，确认后才复制快照并作为 restricted 聊天导入。",
        actions=(
            "/api/chat-imports/chatlab/inspect",
            "/api/chat-imports/chatlab/import",
        ),
    )

    def probe(
        self,
        *,
        location: str | None = None,
        records_path: str | None = None,
        weflow_root: str | None = None,
    ) -> dict[str, Any]:
        source, path_state = _existing_path(location)
        if path_state == "not_configured":
            return {
                "provider": self.manifest.as_dict(),
                "status": "needs_file",
                "content_read": False,
                "next_step": "选择一个 ChatLab JSON 文件后进行只读结构检查。",
            }
        valid_file = bool(
            path_state == "available"
            and source is not None
            and source.is_file()
            and source.suffix.lower() == ".json"
        )
        return {
            "provider": self.manifest.as_dict(),
            "status": "ready_for_inspection" if valid_file else "invalid_chatlab_file",
            "content_read": False,
            "next_step": (
                "文件类型符合预期；下一步只读检查 ChatLab 结构。"
                if valid_file
                else "请选择一个现存的 .json 聊天导出文件。"
            ),
        }


class ImportProviderRegistry:
    """Registry for reviewed, built-in adapters; it never executes uploaded code."""

    def __init__(
        self,
        *,
        settings: Settings,
        sync: SyncService,
        weflow: WeFlowService,
    ) -> None:
        # ``weflow`` is intentionally retained in the construction boundary so
        # a future reviewed provider can delegate only to its explicit
        # inspect/import APIs. No provider probe calls it or reads chat data.
        del weflow
        self._providers: dict[str, ImportProvider] = {}
        self.register(LocalFilesProvider())
        self.register(ObsidianVaultProvider(sync))
        self.register(WeFlowLegacyExportProvider(settings))
        self.register(ChatLabFileProvider())

    def register(self, provider: ImportProvider) -> None:
        provider_id = provider.manifest.provider_id
        if not provider_id or provider_id in self._providers:
            raise ImportProviderError("导入适配器标识无效或重复。")
        self._providers[provider_id] = provider

    def list_providers(self) -> list[dict[str, Any]]:
        return [provider.manifest.as_dict() for provider in self._providers.values()]

    def probe(
        self,
        provider_id: str,
        *,
        location: str | None = None,
        records_path: str | None = None,
        weflow_root: str | None = None,
    ) -> dict[str, Any]:
        return self._provider(provider_id).probe(
            location=location,
            records_path=records_path,
            weflow_root=weflow_root,
        )

    def register_obsidian_vault(
        self,
        *,
        vault_path: str,
        name: str,
        domain: str,
        privacy: str,
        sync_mode: str,
        recursive: bool,
    ) -> dict[str, Any]:
        provider = self._provider("obsidian-vault")
        if not isinstance(provider, ObsidianVaultProvider):
            raise ImportProviderError("Obsidian 适配器当前不可用。")
        return provider.register(
            vault_path=vault_path,
            name=name,
            domain=domain,
            privacy=privacy,
            sync_mode=sync_mode,
            recursive=recursive,
        )

    def _provider(self, provider_id: str) -> ImportProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ImportProviderNotFound("未找到该导入适配器。")
        return provider
