import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient

from pkas.api import LocalKnowledgeServiceMiddleware, create_app
from pkas.db import Database
from pkas.rpa_bridge import (
    RPA_API_PREFIX,
    RPA_MAX_REQUEST_BYTES,
    RpaBridgeService,
    require_loopback_bind_host,
)


def _enable_bridge(
    test_settings,
    monkeypatch,
    *,
    token: str = "test-rpa-token-keep-private",
) -> None:
    monkeypatch.setattr("pkas.rpa_bridge.load_user_secret", lambda *_args: token)
    bridge = RpaBridgeService(test_settings, Database(test_settings))
    bridge.configure(
        enabled=True,
        allow_restricted_context=False,
        retrieval_mode="a",
    )


def _client(settings):
    return TestClient(create_app(settings), client=("127.0.0.1", 51234))


def test_rpa_bridge_is_closed_by_default(test_settings) -> None:
    with _client(test_settings) as client:
        response = client.get("/api/integrations/rpa/v1/status")
    assert response.status_code == 404


def test_rpa_bridge_requires_loopback_and_bearer_token(test_settings, monkeypatch) -> None:
    _enable_bridge(test_settings, monkeypatch)
    headers = {"Authorization": "Bearer test-rpa-token-keep-private"}
    with _client(test_settings) as client:
        assert client.get("/api/integrations/rpa/v1/status").status_code == 401
        assert client.get(
            "/api/integrations/rpa/v1/status",
            headers={"Authorization": "Bearer incorrect"},
        ).status_code == 401
        assert client.get(
            "/api/integrations/rpa/v1/status",
            headers={**headers, "Origin": "https://unexpected.example"},
        ).status_code == 401
        ready = client.get("/api/integrations/rpa/v1/status", headers=headers)
    assert ready.status_code == 200
    assert ready.headers["cache-control"] == "no-store"
    body = ready.json()
    assert body["enabled"] is True
    assert body["token_configured"] is True
    assert "test-rpa-token-keep-private" not in repr(body)

    with TestClient(create_app(test_settings), client=("198.51.100.9", 51234)) as remote_client:
        rejected = remote_client.get("/api/integrations/rpa/v1/status", headers=headers)
    assert rejected.status_code == 403


def test_rpa_bridge_rejects_oversized_http_body(test_settings, monkeypatch) -> None:
    _enable_bridge(test_settings, monkeypatch)
    headers = {
        "Authorization": "Bearer test-rpa-token-keep-private",
        "Content-Type": "application/json",
    }
    body = json.dumps({"padding": "x" * (33 * 1024)})
    with _client(test_settings) as client:
        response = client.post("/api/integrations/rpa/v1/messages", headers=headers, content=body)
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"


def test_rpa_bridge_rejects_a_streamed_oversized_body_without_content_length() -> None:
    downstream_called: list[bool] = []
    sent: list[dict] = []

    async def downstream(_scope, _receive, _send) -> None:
        downstream_called.append(True)

    async def run() -> None:
        messages = iter(
            [
                {
                    "type": "http.request",
                    "body": b"x" * (RPA_MAX_REQUEST_BYTES + 1),
                    "more_body": False,
                }
            ]
        )

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        middleware = LocalKnowledgeServiceMiddleware(
            downstream,
            max_rpa_request_bytes=RPA_MAX_REQUEST_BYTES,
        )
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "path": f"{RPA_API_PREFIX}/messages",
                "raw_path": f"{RPA_API_PREFIX}/messages".encode(),
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 51234),
                "server": ("127.0.0.1", 8765),
            },
            receive,
            send,
        )

    asyncio.run(run())
    assert downstream_called == []
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_rpa_bridge_records_idempotently_and_redacts_secrets(test_settings, monkeypatch) -> None:
    _enable_bridge(test_settings, monkeypatch)
    headers = {"Authorization": "Bearer test-rpa-token-keep-private"}
    payload = {
        "conversation_key": "rpa-test-conversation",
        "source_event_id": "message-001",
        "sender_key": "sender-001",
        "message_at": "2026-09-21T10:00:00+08:00",
        "text": "请记录 token=api-secret-redaction-fixture-value-1234567890",
        "metadata": {
            "channel": "fixture",
            "Authorization": "Bearer rpa-secret-metadata-value-123456",
        },
    }
    with _client(test_settings) as client:
        first = client.post("/api/integrations/rpa/v1/messages", headers=headers, json=payload)
        second = client.post("/api/integrations/rpa/v1/messages", headers=headers, json=payload)
        invalid = client.post(
            "/api/integrations/rpa/v1/messages",
            headers=headers,
            json={**payload, "sql": "SELECT * FROM sources"},
        )
    assert first.status_code == 200
    assert first.json()["status"] == "stored"
    assert first.json()["privacy"] == "restricted"
    assert first.json()["redacted"] is True
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert invalid.status_code == 422
    with sqlite3.connect(test_settings.database_path) as connection:
        rows = connection.execute("SELECT text_content FROM rpa_bridge_messages").fetchall()
    assert len(rows) == 1
    assert "api-secret-redaction-fixture" not in rows[0][0]
    assert "[REDACTED]" in rows[0][0]
    with sqlite3.connect(test_settings.database_path) as connection:
        metadata = connection.execute("SELECT metadata_json FROM rpa_bridge_messages").fetchone()[0]
    assert "rpa-secret-metadata-value" not in metadata


def test_rpa_bridge_returns_bounded_evidence_without_local_paths(
    test_settings, monkeypatch
) -> None:
    _enable_bridge(test_settings, monkeypatch)
    headers = {"Authorization": "Bearer test-rpa-token-keep-private"}
    app = create_app(test_settings)
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        system = app.state.system
        system.ingestion.import_text(
            text="设备维护必须先核对序列号，再确认保修状态。",
            title="设备维护规范",
            original_uri=r"E:\private-material\equipment\support.md",
            source_type="manual-note",
            domain="work",
            privacy="private",
        )
        system.ingestion.import_text(
            text="这条 restricted 内容不能默认返回给 RPA。",
            title="受限资料",
            original_uri=r"E:\private-material\restricted.md",
            source_type="manual-note",
            domain="work",
            privacy="restricted",
        )
        response = client.post(
            "/api/integrations/rpa/v1/search",
            headers=headers,
            json={"query": "序列号保修", "scopes": ["files"], "limit": 8},
        )
        chats = client.post(
            "/api/integrations/rpa/v1/search",
            headers=headers,
            json={"query": "序列号保修", "scopes": ["files", "chats"]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result_count"] == 1
    assert body["mode"] == "a_lexical_catalog"
    evidence = body["evidence"][0]
    assert evidence["title"] == "设备维护规范"
    assert evidence["source_id"]
    assert "original_uri" not in evidence
    assert "vault_path" not in evidence
    assert "E:\\private-material" not in repr(body)
    assert "restricted" not in evidence["excerpt"]
    assert RpaBridgeService.compact_evidence({"locator": r"E:\\private-material\\raw.txt"})[
        "locator"
    ] == "local-section"
    path_evidence = RpaBridgeService.compact_evidence(
        {
            "title": r"E:\\private-material\\support.md",
            "parent_context": {"text": r"资料位于 C:\\private-material\\notes.md"},
        }
    )
    assert "E:\\private-material" not in repr(path_evidence)
    assert "C:\\private-material" not in repr(path_evidence)
    assert path_evidence["content_redacted"] is True
    assert chats.status_code == 200
    assert chats.json()["effective_scopes"] == ["files"]
    assert any("聊天资料未被授权" in item for item in chats.json()["warnings"])


def test_rpa_bridge_rejects_non_loopback_bind_hosts() -> None:
    assert require_loopback_bind_host("127.0.0.1") == "127.0.0.1"
    assert require_loopback_bind_host("::1") == "::1"
    try:
        require_loopback_bind_host("0.0.0.0")
    except ValueError as exc:
        assert "局域网或公网" in str(exc)
    else:
        raise AssertionError("0.0.0.0 must be rejected")


def test_rpa_bridge_schema_migrates_an_existing_database(test_settings) -> None:
    database = Database(test_settings)
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP INDEX idx_rpa_bridge_messages_conversation_time")
        connection.execute("DROP TABLE rpa_bridge_messages")
        connection.execute("UPDATE app_meta SET value='20' WHERE key='schema_version'")
        connection.commit()
    database.initialize()
    with database.connect() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rpa_bridge_messages'"
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    assert table is not None
    assert version == "21"
