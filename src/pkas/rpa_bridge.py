"""Narrow localhost-only adapter for desktop RPA products.

The adapter deliberately does not expose SQLite, filesystem paths, source
documents, or a generic query endpoint.  It records one RPA message at a time
as a restricted operational trace and returns bounded, provenance-aware
retrieval evidence through a separately authenticated HTTP route.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pkas.codex_capture import redact_secrets
from pkas.config import Settings
from pkas.content_taxonomy import atomic_write
from pkas.db import Database
from pkas.local_secrets import LocalSecretError, load_user_secret, save_user_secret

RPA_API_PREFIX = "/api/integrations/rpa/v1"
RPA_SECRET_NAME = "rpa_loopback_token"
RPA_CONFIG_NAME = "rpa-loopback.json"
RPA_MAX_REQUEST_BYTES = 32 * 1024
RPA_MAX_EVIDENCE_CHARS = 1_200

# The bridge is deliberately conservative about material returned to an
# external desktop/RPA process.  A source title or a document excerpt can
# contain an absolute path even when the explicit ``locator`` field does not.
# Keep the adapter from becoming an accidental file-system inventory endpoint.
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_/])(?:[A-Za-z]:[\\/]|\\\\[^\\/:*?\"<>|\s]+\\)[^\r\n\t\"<>|]*"
)
_UNIX_ABSOLUTE_PATH = re.compile(
    r"(?<![:A-Za-z0-9_])/(?:Users|home|var|tmp|etc|opt|mnt|private|Volumes)(?:/[^\r\n\t\"<>]*)?"
)


class RpaBridgeError(RuntimeError):
    """Base error that never contains token or request-body values."""


class RpaBridgeDisabled(RpaBridgeError):
    pass


class RpaBridgeUnauthorized(RpaBridgeError):
    pass


class RpaBridgeRateLimited(RpaBridgeError):
    pass


class RpaBridgeUnavailable(RpaBridgeError):
    pass


class RpaBridgeConfigError(RpaBridgeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def is_loopback_host(value: str | None) -> bool:
    """Accept only an IP loopback address or the literal localhost name."""
    if not value:
        return False
    normalized = value.strip().strip("[]").split("%", 1)[0].lower()
    if normalized == "localhost":
        return True
    try:
        address = ipaddress.ip_address(normalized)
        if address.is_loopback:
            return True
        mapped = getattr(address, "ipv4_mapped", None)
        return bool(mapped and mapped.is_loopback)
    except ValueError:
        return False


def require_loopback_bind_host(value: str) -> str:
    """Reject accidental 0.0.0.0/LAN bindings before Uvicorn is started."""
    normalized = value.strip()
    if not is_loopback_host(normalized):
        raise ValueError("知识服务只能监听 127.0.0.1、::1 或 localhost，不能暴露到局域网或公网。")
    return normalized


def _origin_is_loopback(origin: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    return parsed.scheme == "http" and is_loopback_host(parsed.hostname)


def redact_local_paths(value: str) -> tuple[str, bool]:
    """Replace local absolute paths before an RPA result crosses the boundary."""
    without_windows, windows_replacements = _WINDOWS_ABSOLUTE_PATH.subn("[LOCAL_PATH]", value)
    without_unix, unix_replacements = _UNIX_ABSOLUTE_PATH.subn("[LOCAL_PATH]", without_windows)
    return without_unix, bool(windows_replacements or unix_replacements)


@dataclass(frozen=True, slots=True)
class RpaBridgePolicy:
    schema_version: int = 1
    enabled: bool = False
    allow_restricted_context: bool = False
    retrieval_mode: Literal["a", "ab"] = "a"
    max_results: int = 5
    rate_limit_per_minute: int = 120

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RpaBridgePolicy:
        schema_version = value.get("schema_version", 1)
        enabled = value.get("enabled", False)
        allow_restricted = value.get("allow_restricted_context", False)
        retrieval_mode = value.get("retrieval_mode", "a")
        max_results = value.get("max_results", 5)
        rate_limit = value.get("rate_limit_per_minute", 120)
        if schema_version != 1:
            raise RpaBridgeConfigError("RPA 本机桥配置版本不受支持。")
        if not isinstance(enabled, bool) or not isinstance(allow_restricted, bool):
            raise RpaBridgeConfigError("RPA 本机桥配置中的开关无效。")
        if retrieval_mode not in {"a", "ab"}:
            raise RpaBridgeConfigError("RPA 本机桥检索模式无效。")
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 8
        ):
            raise RpaBridgeConfigError("RPA 本机桥结果数量无效。")
        if (
            isinstance(rate_limit, bool)
            or not isinstance(rate_limit, int)
            or not 10 <= rate_limit <= 600
        ):
            raise RpaBridgeConfigError("RPA 本机桥限流配置无效。")
        return cls(
            enabled=enabled,
            allow_restricted_context=allow_restricted,
            retrieval_mode=retrieval_mode,
            max_results=max_results,
            rate_limit_per_minute=rate_limit,
        )


class _LocalRateLimiter:
    """Small in-process ceiling for a local RPA process, without storing tokens."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self, maximum: int) -> None:
        now = self._clock()
        boundary = now - 60.0
        with self._lock:
            while self._events and self._events[0] <= boundary:
                self._events.popleft()
            if len(self._events) >= maximum:
                raise RpaBridgeRateLimited("RPA 本机桥请求过快，请稍后重试。")
            self._events.append(now)


SecretLoader = Callable[[Settings, str], str | None]
SecretSaver = Callable[[Settings, str, str], Path]


class RpaBridgeService:
    """Local RPA integration policy, token guard and restricted message ledger."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        secret_loader: SecretLoader | None = None,
        secret_saver: SecretSaver | None = None,
        rate_limiter: _LocalRateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self._secret_loader = secret_loader or load_user_secret
        self._secret_saver = secret_saver or save_user_secret
        self._rate_limiter = rate_limiter or _LocalRateLimiter()

    @property
    def config_path(self) -> Path:
        return self.settings.data_root / "config" / RPA_CONFIG_NAME

    def _read_policy(self) -> tuple[RpaBridgePolicy, str | None]:
        path = self.config_path
        if not path.is_file():
            return RpaBridgePolicy(), None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise RpaBridgeConfigError("RPA 本机桥配置必须是对象。")
            return RpaBridgePolicy.from_mapping(raw), None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RpaBridgeConfigError):
            # A damaged policy must fail closed rather than retaining a stale
            # enabled state in memory.
            return RpaBridgePolicy(), "invalid"

    def policy(self) -> RpaBridgePolicy:
        return self._read_policy()[0]

    def _load_token(self) -> str | None:
        try:
            value = self._secret_loader(self.settings, RPA_SECRET_NAME)
        except (LocalSecretError, OSError, UnicodeError):
            return None
        return value.strip() if value and value.strip() else None

    def status(self) -> dict[str, Any]:
        policy, config_error = self._read_policy()
        token_present = bool(self._load_token())
        return {
            "contract": "pkas.rpa-loopback.v1",
            "enabled": bool(policy.enabled and token_present and not config_error),
            "token_configured": token_present,
            "network_scope": "loopback_only",
            "endpoint_prefix": RPA_API_PREFIX,
            "retrieval_mode": "hybrid" if policy.retrieval_mode == "ab" else "local_fts",
            "allow_restricted_context": policy.allow_restricted_context,
            "max_results": policy.max_results,
            "rate_limit_per_minute": policy.rate_limit_per_minute,
            "configuration_valid": config_error is None,
            "raw_database_access": False,
            "raw_file_path_returned": False,
        }

    def _write_policy(self, policy: RpaBridgePolicy) -> None:
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            previous = path.read_bytes()
            backup = path.with_suffix(".previous.json")
            atomic_write(backup, previous)
            if backup.read_bytes() != previous:
                raise RpaBridgeConfigError("RPA 本机桥配置恢复点校验失败。")
        atomic_write(path, json.dumps(asdict(policy), ensure_ascii=False, indent=2).encode("utf-8"))

    def configure(
        self,
        *,
        enabled: bool,
        allow_restricted_context: bool,
        retrieval_mode: Literal["a", "ab"],
    ) -> dict[str, Any]:
        if enabled and not self._load_token():
            raise RpaBridgeConfigError("请先创建 RPA 本机桥令牌，再启用接口。")
        policy = RpaBridgePolicy(
            enabled=enabled,
            allow_restricted_context=allow_restricted_context,
            retrieval_mode=retrieval_mode,
        )
        self._write_policy(policy)
        return self.status()

    def create_token(self, *, rotate: bool = False) -> str:
        if self._load_token() and not rotate:
            raise RpaBridgeConfigError("RPA 本机桥令牌已存在；如需失效旧令牌请使用轮换命令。")
        value = secrets.token_urlsafe(32)
        try:
            self._secret_saver(self.settings, RPA_SECRET_NAME, value)
        except (LocalSecretError, OSError, UnicodeError) as exc:
            raise RpaBridgeUnavailable("无法为当前 Windows 用户保存 RPA 本机桥令牌。") from exc
        return value

    def provision(
        self,
        *,
        allow_restricted_context: bool,
        retrieval_mode: Literal["a", "ab"],
    ) -> tuple[str, dict[str, Any]]:
        token = self.create_token()
        try:
            status = self.configure(
                enabled=True,
                allow_restricted_context=allow_restricted_context,
                retrieval_mode=retrieval_mode,
            )
        except Exception as exc:
            # The token itself is protected; leaving the bridge disabled is the
            # safe failure mode if its non-secret policy cannot be saved.
            raise RpaBridgeConfigError("RPA 本机桥令牌已创建，但接口未启用。") from exc
        return token, status

    def authorize(
        self,
        *,
        client_host: str | None,
        authorization: str | None,
        origin: str | None,
    ) -> RpaBridgePolicy:
        if not is_loopback_host(client_host):
            raise RpaBridgeUnauthorized("仅允许本机回环地址访问 RPA 本机桥。")
        if origin and not _origin_is_loopback(origin):
            raise RpaBridgeUnauthorized("RPA 本机桥拒绝非本机浏览器来源。")
        policy, config_error = self._read_policy()
        if config_error or not policy.enabled:
            raise RpaBridgeDisabled("RPA 本机桥未启用。")
        expected = self._load_token()
        if not expected:
            raise RpaBridgeUnavailable("RPA 本机桥令牌不可用。")
        scheme, _, supplied = (authorization or "").partition(" ")
        if (
            scheme.lower() != "bearer"
            or not supplied
            or not hmac.compare_digest(supplied, expected)
        ):
            raise RpaBridgeUnauthorized("RPA 本机桥认证失败。")
        self._rate_limiter.check(policy.rate_limit_per_minute)
        return policy

    @staticmethod
    def _event_key(
        *,
        conversation_key: str,
        source_event_id: str | None,
        sender_key: str | None,
        message_at: str | None,
        text: str,
    ) -> tuple[str, bool]:
        if source_event_id:
            material = f"event\x1f{conversation_key}\x1f{source_event_id}"
            return hashlib.sha256(material.encode("utf-8")).hexdigest(), False
        material = "\x1f".join((conversation_key, sender_key or "", message_at or "", text))
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), True

    @staticmethod
    def _audit(connection, event_type: str, subject_id: str, details: dict[str, Any]) -> None:
        connection.execute(
            """INSERT INTO audit_events(
                event_type, subject_type, subject_id, details_json, created_at
            )
            VALUES (?, 'rpa_bridge', ?, ?, ?)""",
            (event_type, subject_id, json.dumps(details, ensure_ascii=False), utc_now()),
        )

    def record_message(
        self,
        *,
        conversation_key: str,
        text: str,
        source_event_id: str | None,
        sender_key: str | None,
        message_at: str | None,
        metadata: Mapping[str, str],
    ) -> dict[str, Any]:
        safe_conversation_key, conversation_redactions = redact_secrets(
            conversation_key.replace("\x00", "").strip()
        )
        safe_source_event_id, source_event_redactions = redact_secrets(
            (source_event_id or "").replace("\x00", "").strip()
        )
        safe_sender_key, sender_redactions = redact_secrets(
            (sender_key or "").replace("\x00", "").strip()
        )
        safe_message_at, message_at_redactions = redact_secrets(
            (message_at or "").replace("\x00", "").strip()
        )
        safe_text, text_redactions = redact_secrets(text.replace("\x00", "").strip())
        redaction_count = (
            conversation_redactions
            + source_event_redactions
            + sender_redactions
            + message_at_redactions
            + text_redactions
        )
        safe_metadata: dict[str, str] = {}
        for key, value in metadata.items():
            safe_key, key_redactions = redact_secrets(str(key).replace("\x00", "").strip())
            safe_value, value_redactions = redact_secrets(str(value).replace("\x00", "").strip())
            safe_metadata[safe_key] = safe_value
            redaction_count += key_redactions + value_redactions
        event_key, generated_key = self._event_key(
            conversation_key=safe_conversation_key,
            source_event_id=safe_source_event_id or None,
            sender_key=safe_sender_key or None,
            message_at=safe_message_at or None,
            text=safe_text,
        )
        received_at = utc_now()
        message_id = "rpa_" + uuid.uuid4().hex
        content_hash = hashlib.sha256(safe_text.encode("utf-8")).hexdigest()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO rpa_bridge_messages(
                    id, event_key, conversation_key, sender_key, message_at,
                    text_content, content_hash, metadata_json, redaction_count, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    event_key,
                    safe_conversation_key,
                    safe_sender_key or None,
                    safe_message_at or None,
                    safe_text,
                    content_hash,
                    json.dumps(safe_metadata, ensure_ascii=False, separators=(",", ":")),
                    redaction_count,
                    received_at,
                ),
            )
            stored = cursor.rowcount == 1
            if stored:
                self._audit(
                    connection,
                    "rpa_loopback_message_stored",
                    message_id,
                    {
                        "conversation_key": safe_conversation_key,
                        "source_event_id_supplied": bool(safe_source_event_id),
                        "redaction_count": redaction_count,
                        "content_chars": len(safe_text),
                    },
                )
            else:
                existing = connection.execute(
                    """SELECT id, received_at, redaction_count
                    FROM rpa_bridge_messages WHERE event_key=?""",
                    (event_key,),
                ).fetchone()
                if existing:
                    message_id = str(existing["id"])
                    received_at = str(existing["received_at"])
                    redaction_count = int(existing["redaction_count"])
            connection.commit()
        return {
            "message_id": message_id,
            "status": "stored" if stored else "duplicate",
            "received_at": received_at,
            "privacy": "restricted",
            "redacted": bool(redaction_count),
            "generated_idempotency_key": generated_key,
        }

    def audit_search(
        self,
        *,
        result_count: int,
        scopes: Sequence[str],
        retrieval_mode: str,
    ) -> None:
        with self.database.connect() as connection:
            self._audit(
                connection,
                "rpa_loopback_search",
                "rpa-search",
                {
                    "result_count": result_count,
                    "scopes": scopes,
                    "retrieval_mode": retrieval_mode,
                },
            )
            connection.commit()

    @staticmethod
    def compact_evidence(item: Mapping[str, Any]) -> dict[str, Any]:
        parent = item.get("parent_context")
        raw_excerpt = ""
        if isinstance(parent, Mapping):
            raw_excerpt = str(parent.get("text") or "")
        if not raw_excerpt:
            raw_excerpt = str(item.get("snippet") or "")
        excerpt, redactions = redact_secrets(raw_excerpt.replace("\x00", "").strip())
        excerpt, excerpt_path_redacted = redact_local_paths(excerpt)
        excerpt_truncated = len(excerpt) > RPA_MAX_EVIDENCE_CHARS
        if excerpt_truncated:
            excerpt = excerpt[:RPA_MAX_EVIDENCE_CHARS].rstrip() + "\n[片段已截断]"
        title, title_redactions = redact_secrets(str(item.get("title") or "未命名资料"))
        title, title_path_redacted = redact_local_paths(title)
        locator, locator_redactions = redact_secrets(str(item.get("locator") or ""))
        # A locator is useful for a reply draft, but an absolute Windows/Unix
        # path would reveal more than this narrow adapter is meant to disclose.
        locator = locator.strip()
        locator, locator_path_redacted = redact_local_paths(locator)
        if locator_path_redacted or locator.startswith(("/", "\\\\")):
            locator = "local-section"
        channels = item.get("retrieval_channels") or []
        if not isinstance(channels, list):
            channels = []
        return {
            "evidence_id": str(item.get("chunk_id") or item.get("document_id") or ""),
            "source_id": str(item.get("source_id") or "") or None,
            "title": title[:240],
            "locator": locator[:240] or None,
            "excerpt": excerpt,
            "excerpt_truncated": excerpt_truncated,
            "domain": item.get("domain"),
            "source_type": item.get("source_type"),
            "retrieval_channels": [str(channel) for channel in channels[:3]],
            "evidence_status": item.get("evidence_status") or "source_backed",
            "content_redacted": bool(
                redactions
                + title_redactions
                + locator_redactions
                + excerpt_path_redacted
                + title_path_redacted
                + locator_path_redacted
            ),
        }
