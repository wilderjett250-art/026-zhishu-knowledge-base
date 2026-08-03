import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from pkas.config import Settings, get_settings
from pkas.customer_repository import CustomerRepository
from pkas.ingest import ImportBoundaryError, IngestionService


class WeFlowError(RuntimeError):
    pass


class WeFlowFormatError(ValueError):
    pass


class WeFlowClient:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = self.validate_base_url(base_url)
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
            transport=transport,
        )

    @staticmethod
    def validate_base_url(value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            raise WeFlowError("WeFlow 地址必须使用 http 或 https。")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise WeFlowError("WeFlow 连接器默认只允许访问本机地址。")
        if parsed.username or parsed.password:
            raise WeFlowError("WeFlow 地址中不能包含用户名或密码。")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise WeFlowError("WeFlow 地址必须是本地 API 根地址，不能包含路径、查询或片段。")
        return normalized

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WeFlowClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.ConnectError as exc:
            raise WeFlowError("无法连接 WeFlow，请确认本地 API 服务已经开启。") from exc
        except httpx.TimeoutException as exc:
            raise WeFlowError("WeFlow 请求超时，请缩小单次同步范围后重试。") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise WeFlowError("WeFlow Access Token 无效或权限不足。") from exc
            raise WeFlowError(f"WeFlow 返回 HTTP {status}。") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise WeFlowError("WeFlow 返回了无法解析的 JSON。") from exc
        if not isinstance(payload, dict):
            raise WeFlowError("WeFlow 返回结构不是 JSON 对象。")
        if payload.get("success") is False:
            raise WeFlowError(str(payload.get("error") or "WeFlow 请求失败。"))
        return payload

    def health(self) -> dict[str, Any]:
        return self._get("/api/v1/health")

    def sessions(self, keyword: str = "", limit: int = 500) -> list[dict[str, Any]]:
        payload = self._get(
            "/api/v1/sessions",
            {"format": "chatlab", "keyword": keyword, "limit": max(1, min(limit, 5000))},
        )
        sessions = payload.get("sessions", [])
        if not isinstance(sessions, list):
            raise WeFlowError("WeFlow 会话列表结构无效。")
        return [item for item in sessions if isinstance(item, dict)]

    def contacts(self, keyword: str = "", limit: int = 100) -> list[dict[str, Any]]:
        payload = self._get(
            "/api/v1/contacts",
            {"keyword": keyword, "limit": max(1, min(limit, 5000))},
        )
        contacts = payload.get("contacts", [])
        if not isinstance(contacts, list):
            return []
        return [item for item in contacts if isinstance(item, dict)]

    def pull_messages(
        self,
        session_id: str,
        *,
        since: int = 0,
        end: int | None = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = self._get(
            f"/api/v1/sessions/{quote(session_id, safe='')}/messages",
            {
                "since": max(0, since),
                "end": end or int(time.time()),
                "limit": max(1, min(limit, 5000)),
                "offset": max(0, offset),
            },
        )
        validate_chatlab(payload)
        return payload


def validate_chatlab(payload: dict[str, Any]) -> None:
    chatlab = payload.get("chatlab")
    meta = payload.get("meta")
    messages = payload.get("messages")
    if (
        not isinstance(chatlab, dict)
        or not isinstance(meta, dict)
        or not isinstance(messages, list)
    ):
        raise WeFlowFormatError("文件不是有效的 ChatLab 会话结构。")
    generator = str(chatlab.get("generator", ""))
    if generator.lower() != "weflow":
        raise WeFlowFormatError("ChatLab 文件不是由 WeFlow 生成的。")
    if str(meta.get("platform", "wechat")).lower() != "wechat":
        raise WeFlowFormatError("ChatLab 文件不是微信会话。")


def detect_session_id(payload: dict[str, Any], fallback: str | None = None) -> str:
    meta = payload.get("meta", {})
    for key in ("groupId", "contactId", "sessionId", "talker", "id"):
        value = meta.get(key) if isinstance(meta, dict) else None
        if value:
            return str(value)
    owner_id = str(meta.get("ownerId", "")) if isinstance(meta, dict) else ""
    senders = {
        str(message.get("sender"))
        for message in payload.get("messages", [])
        if isinstance(message, dict)
        and message.get("sender")
        and str(message.get("sender")) != owner_id
    }
    if len(senders) == 1:
        return senders.pop()
    if fallback:
        return fallback
    raise WeFlowFormatError("无法确定私聊会话 ID，请明确填写微信 wxid。")


class WeFlowService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        customers: CustomerRepository,
        ingestion: IngestionService,
    ) -> None:
        self.settings = settings or get_settings()
        self.customers = customers
        self.ingestion = ingestion

    def _client(
        self,
        base_url: str,
        access_token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> WeFlowClient:
        return WeFlowClient(
            base_url=base_url,
            access_token=access_token,
            timeout=self.settings.weflow_timeout_seconds,
            transport=transport,
        )

    def check_connection(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> dict[str, Any]:
        with self._client(base_url, access_token, transport) as client:
            health = client.health()
            connector_id = self.customers.upsert_connector(
                connector_type="weflow",
                name="WeFlow 本地微信数据源",
                base_url=client.base_url,
                status="online",
                config={"transport": "local-http", "token_stored": False},
                healthy=True,
            )
        return {
            "connector_id": connector_id,
            "base_url": WeFlowClient.validate_base_url(base_url),
            "health": health,
            "token_stored": False,
        }

    def list_sessions(
        self,
        *,
        base_url: str,
        access_token: str,
        keyword: str = "",
        limit: int = 500,
        transport: httpx.BaseTransport | None = None,
    ) -> list[dict[str, Any]]:
        with self._client(base_url, access_token, transport) as client:
            return client.sessions(keyword, limit)

    def sync_sessions(
        self,
        *,
        base_url: str,
        access_token: str,
        session_ids: list[str],
        incremental: bool = True,
        privacy: str = "restricted",
        max_messages_per_session: int = 50000,
        transport: httpx.BaseTransport | None = None,
    ) -> dict[str, Any]:
        if not session_ids:
            raise WeFlowError("至少选择一个 WeFlow 会话。")
        if len(session_ids) > 100:
            raise WeFlowError("单次最多同步 100 个会话。")
        results = []
        with self._client(base_url, access_token, transport) as client:
            connector_id = self.customers.upsert_connector(
                connector_type="weflow",
                name="WeFlow 本地微信数据源",
                base_url=client.base_url,
                status="online",
                config={"transport": "local-http", "token_stored": False},
                healthy=True,
            )
            for session_id in session_ids:
                results.append(
                    self._sync_one_session(
                        client=client,
                        connector_id=connector_id,
                        session_id=session_id,
                        incremental=incremental,
                        privacy=privacy,
                        max_messages=max_messages_per_session,
                    )
                )
        return {
            "connector_id": connector_id,
            "sessions": results,
            "imported": sum(item["imported"] for item in results),
            "duplicates": sum(item["duplicates"] for item in results),
            "snapshot_paths": [path for item in results for path in item["snapshot_paths"]],
            "token_stored": False,
        }

    def _sync_one_session(
        self,
        *,
        client: WeFlowClient,
        connector_id: str,
        session_id: str,
        incremental: bool,
        privacy: str,
        max_messages: int,
    ) -> dict[str, Any]:
        cursor = self.customers.get_sync_cursor(session_id)
        if incremental and cursor["offset"] > 0:
            since = cursor["since"]
            offset = cursor["offset"]
            end = cursor["watermark"] or int(time.time())
        else:
            since = cursor["watermark"] if incremental else 0
            offset = 0
            end = int(time.time())
        watermark = cursor["watermark"] if incremental else 0
        imported = 0
        duplicates = 0
        pages = 0
        snapshot_paths: list[str] = []
        has_more = False
        contact = self._find_contact(client, session_id)
        last_cursor = (-1, -1)
        while imported + duplicates < max_messages:
            remaining = max_messages - imported - duplicates
            payload = client.pull_messages(
                session_id,
                since=since,
                end=end,
                limit=min(5000, remaining),
                offset=offset,
            )
            snapshot = self._store_snapshot(
                payload,
                source_uri=f"weflow://session/{session_id}",
                metadata={"session_id": session_id, "page": pages + 1},
            )
            result = self.customers.ingest_chatlab_page(
                payload=payload,
                session_id=session_id,
                connector_id=connector_id,
                snapshot=snapshot,
                privacy=privacy,
                contact=contact,
            )
            imported += result["imported"]
            duplicates += result["duplicates"]
            pages += 1
            snapshot_paths.append(snapshot["vault_path"])
            sync = payload.get("sync", {}) if isinstance(payload.get("sync"), dict) else {}
            watermark = int(sync.get("watermark") or watermark or end)
            has_more = bool(sync.get("hasMore"))
            if not has_more:
                since = watermark
                offset = 0
                break
            next_since = int(sync.get("nextSince") or since)
            next_offset = int(sync.get("nextOffset") or 0)
            next_cursor = (next_since, next_offset)
            if next_cursor == last_cursor or next_cursor == (since, offset):
                raise WeFlowError(f"会话 {session_id} 的增量游标没有前进，已安全停止。")
            last_cursor = (since, offset)
            since, offset = next_cursor
        self.customers.update_sync_cursor(
            session_id,
            since=since,
            offset=offset,
            watermark=watermark,
        )
        return {
            "session_id": session_id,
            "imported": imported,
            "duplicates": duplicates,
            "pages": pages,
            "watermark": watermark,
            "snapshot_paths": snapshot_paths,
            "limited": has_more and imported + duplicates >= max_messages,
        }

    @staticmethod
    def _find_contact(client: WeFlowClient, session_id: str) -> dict[str, Any] | None:
        if session_id.endswith("@chatroom"):
            return None
        for contact in client.contacts(session_id, 20):
            if str(contact.get("username")) == session_id:
                return contact
        return None

    def inspect_chatlab_file(
        self,
        path: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        inspection = self.ingestion.inspect_path(path, recursive=False)
        target = Path(inspection["path"])
        if not target.is_file() or target.suffix.lower() != ".json":
            raise WeFlowFormatError("请选择一个 WeFlow ChatLab JSON 文件。")
        if inspection["total_bytes"] > self.settings.max_source_bytes:
            raise WeFlowFormatError(
                f"ChatLab 文件超过单文件上限 {self.settings.max_source_bytes} 字节。"
            )
        payload = self._load_chatlab(target)
        detected_session_id = detect_session_id(payload, session_id)
        return {
            **inspection,
            "session_id": detected_session_id,
            "name": payload.get("meta", {}).get("name") or detected_session_id,
            "conversation_type": payload.get("meta", {}).get("type") or "private",
            "message_count": len(payload.get("messages", [])),
            "chatlab_version": payload.get("chatlab", {}).get("version"),
            "generator": payload.get("chatlab", {}).get("generator"),
        }

    def import_chatlab_file(
        self,
        *,
        path: str,
        inspection_token: str,
        session_id: str | None = None,
        privacy: str = "restricted",
    ) -> dict[str, Any]:
        inspection = self.inspect_chatlab_file(path, session_id=session_id)
        if inspection["inspection_token"] != inspection_token:
            raise ImportBoundaryError("ChatLab 文件在检查后发生了变化，请重新检查后再确认导入。")
        target = Path(inspection["path"])
        payload = self._load_chatlab(target)
        detected_session_id = detect_session_id(payload, session_id)
        connector_id = self.customers.upsert_connector(
            connector_type="weflow-chatlab",
            name="WeFlow ChatLab 离线导入",
            base_url="offline://weflow-chatlab",
            status="ready",
            config={"transport": "file", "token_stored": False},
        )
        snapshot = self._store_snapshot(
            payload,
            source_uri=str(target.resolve()),
            metadata={"session_id": detected_session_id, "offline_import": True},
        )
        result = self.customers.ingest_chatlab_page(
            payload=payload,
            session_id=detected_session_id,
            connector_id=connector_id,
            snapshot=snapshot,
            privacy=privacy,
        )
        watermark = max(
            (int(message.get("timestamp") or 0) for message in payload.get("messages", [])),
            default=0,
        )
        self.customers.update_sync_cursor(
            detected_session_id,
            since=watermark,
            offset=0,
            watermark=watermark,
        )
        return {
            **result,
            "session_id": detected_session_id,
            "snapshot_path": snapshot["vault_path"],
            "token_stored": False,
        }

    @staticmethod
    def _load_chatlab(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WeFlowFormatError(f"无法读取 WeFlow ChatLab JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise WeFlowFormatError("ChatLab 文件顶层必须是 JSON 对象。")
        validate_chatlab(payload)
        return payload

    def _store_snapshot(
        self,
        payload: dict[str, Any],
        *,
        source_uri: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        content_hash = hashlib.sha256(encoded).hexdigest()
        vault_dir = self.settings.data_root / "raw" / "weflow" / "sha256" / content_hash[:2]
        vault_dir.mkdir(parents=True, exist_ok=True)
        vault_path = vault_dir / f"{content_hash}.json"
        if not vault_path.exists():
            vault_path.write_bytes(encoded)
        if hashlib.sha256(vault_path.read_bytes()).hexdigest() != content_hash:
            raise OSError("WeFlow 快照写入后的哈希校验失败。")
        return {
            "content_hash": content_hash,
            "vault_path": str(vault_path.resolve()),
            "byte_size": len(encoded),
            "source_uri": source_uri,
            "metadata": metadata,
        }
