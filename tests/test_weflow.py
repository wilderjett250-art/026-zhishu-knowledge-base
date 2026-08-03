import json
from pathlib import Path

import httpx

from pkas.system import KnowledgeSystem

FIXTURE = Path(__file__).parent / "fixtures" / "weflow_chatlab_private.json"


def copy_fixture(source_root: Path) -> Path:
    target = source_root / "weflow-customer.json"
    target.write_bytes(FIXTURE.read_bytes())
    return target


def test_offline_chatlab_import_customer_search_and_context(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    chatlab = copy_fixture(source_root)
    inspection = knowledge_system.weflow.inspect_chatlab_file(str(chatlab))
    assert inspection["session_id"] == "wxid_customer_demo"
    assert inspection["message_count"] == 3
    assert inspection["generator"] == "WeFlow"

    imported = knowledge_system.customer_workflows.import_chatlab(
        path=str(chatlab),
        inspection_token=inspection["inspection_token"],
        session_id=None,
        privacy="restricted",
    )
    assert imported["status"] == "completed"
    assert imported["result"]["imported"] == 3
    assert Path(imported["result"]["snapshot_path"]).is_file()

    customers = knowledge_system.customers.list_customers()
    assert len(customers) == 1
    customer = customers[0]
    assert customer["display_name"] == "测试客户张经理"
    assert customer["message_count"] == 3

    assert knowledge_system.customers.timeline(customer["id"]) == []
    timeline = knowledge_system.customers.timeline(
        customer["id"],
        include_restricted=True,
    )
    assert len(timeline) == 3
    assert timeline[0]["content"] == "还需要包含三家门店的数据看板。"
    assert timeline[1]["is_self"] == 1
    assert timeline[0]["source_uri"] == str(chatlab.resolve())

    results = knowledge_system.customers.search_messages(
        "三家门店 数据看板",
        customer_id=customer["id"],
        include_restricted=True,
    )
    assert len(results) == 1
    assert results[0]["platform_message_id"] == "wf-demo-003"

    signal = knowledge_system.customers.create_signal(
        customer_id=customer["id"],
        signal_type="requirement",
        statement="客户要求包含三家门店的数据看板。",
        status="open",
        due_at=None,
        evidence_message_ids=[results[0]["message_id"]],
        confidence="high",
    )
    assert signal is not None
    assert knowledge_system.customers.review_signal(signal["id"], "approved", "证据已核对")

    context = knowledge_system.customer_service.prepare_reply_context(
        customer_id=customer["id"],
        task="回复客户关于门店数据看板和报价的问题",
        include_restricted=True,
    )
    assert context is not None
    assert len(context["recent_messages"]) == 3
    assert len(context["approved_signals"]) == 1
    assert context["customer"]["platform_id"] == "wxid_customer_demo"

    duplicate = knowledge_system.customer_workflows.import_chatlab(
        path=str(chatlab),
        inspection_token=inspection["inspection_token"],
        session_id=None,
        privacy="restricted",
    )
    assert duplicate["result"]["imported"] == 0
    assert duplicate["result"]["duplicates"] == 3


def test_weflow_local_api_connector_and_incremental_deduplication(
    knowledge_system: KnowledgeSystem,
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("Authorization", ""))
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "sessions": [
                        {
                            "id": "wxid_customer_demo",
                            "name": "测试客户张经理",
                            "platform": "wechat",
                            "type": "private",
                            "messageCount": 3,
                            "lastMessageAt": 1738713720,
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/contacts":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "contacts": [
                        {
                            "username": "wxid_customer_demo",
                            "displayName": "张经理",
                            "remark": "客户-张经理",
                            "nickname": "张经理",
                            "alias": "demo-customer",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/messages"):
            since = int(request.url.params.get("since", "0"))
            if since >= 1738713900:
                empty = {**payload, "messages": []}
                return httpx.Response(200, json=empty)
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    health = knowledge_system.weflow.check_connection(
        base_url="http://127.0.0.1:5031",
        access_token="temporary-test-token",
        transport=transport,
    )
    assert health["health"]["status"] == "ok"
    sessions = knowledge_system.weflow.list_sessions(
        base_url="http://127.0.0.1:5031",
        access_token="temporary-test-token",
        transport=transport,
    )
    assert sessions[0]["id"] == "wxid_customer_demo"

    first = knowledge_system.weflow.sync_sessions(
        base_url="http://127.0.0.1:5031",
        access_token="temporary-test-token",
        session_ids=["wxid_customer_demo"],
        transport=transport,
    )
    second = knowledge_system.weflow.sync_sessions(
        base_url="http://127.0.0.1:5031",
        access_token="temporary-test-token",
        session_ids=["wxid_customer_demo"],
        transport=transport,
    )
    assert first["imported"] == 3
    assert second["imported"] == 0
    assert first["token_stored"] is False
    assert all(value == "Bearer temporary-test-token" for value in seen_authorization)

    with knowledge_system.database.connect() as connection:
        connector = connection.execute(
            "SELECT config_json FROM connectors WHERE connector_type = 'weflow'"
        ).fetchone()
        snapshot_paths = [
            row["vault_path"]
            for row in connection.execute("SELECT vault_path FROM connector_snapshots")
        ]
    assert "temporary-test-token" not in connector["config_json"]
    assert all(
        "temporary-test-token" not in Path(path).read_text(encoding="utf-8")
        for path in snapshot_paths
    )


def test_weflow_incremental_sync_resumes_an_unfinished_page_cursor(
    knowledge_system: KnowledgeSystem,
) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    messages = payload["messages"][:2]
    requested_pages: list[tuple[int, int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/contacts":
            return httpx.Response(200, json={"success": True, "contacts": []})
        if request.url.path.endswith("/messages"):
            since = int(request.url.params["since"])
            end = int(request.url.params["end"])
            offset = int(request.url.params["offset"])
            requested_pages.append((since, end, offset))
            if since >= 1738713900 and offset == 0:
                return httpx.Response(
                    200,
                    json={
                        **payload,
                        "messages": [],
                        "sync": {
                            "hasMore": False,
                            "nextSince": 1738713900,
                            "nextOffset": 0,
                            "watermark": 1738713900,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    **payload,
                    "messages": [messages[offset]],
                    "sync": {
                        "hasMore": offset == 0,
                        "nextSince": 0 if offset == 0 else 1738713900,
                        "nextOffset": 1 if offset == 0 else 0,
                        "watermark": 1738713900,
                    },
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    first = knowledge_system.weflow.sync_sessions(
        base_url="http://127.0.0.1:5031",
        access_token="temporary-test-token",
        session_ids=["wxid_customer_demo"],
        max_messages_per_session=1,
        transport=transport,
    )
    cursor_after_first = knowledge_system.customers.get_sync_cursor(
        "wxid_customer_demo"
    )
    second = knowledge_system.weflow.sync_sessions(
        base_url="http://127.0.0.1:5031",
        access_token="temporary-test-token",
        session_ids=["wxid_customer_demo"],
        transport=transport,
    )
    cursor_after_second = knowledge_system.customers.get_sync_cursor(
        "wxid_customer_demo"
    )
    third = knowledge_system.weflow.sync_sessions(
        base_url="http://127.0.0.1:5031",
        access_token="temporary-test-token",
        session_ids=["wxid_customer_demo"],
        transport=transport,
    )

    assert first["imported"] == 1
    assert first["sessions"][0]["limited"] is True
    assert cursor_after_first == {"since": 0, "offset": 1, "watermark": 1738713900}
    assert second["imported"] == 1
    assert second["sessions"][0]["limited"] is False
    assert cursor_after_second == {
        "since": 1738713900,
        "offset": 0,
        "watermark": 1738713900,
    }
    assert third["imported"] == 0
    assert requested_pages[1] == (0, 1738713900, 1)
    assert requested_pages[2][0] == 1738713900


def test_weflow_rejects_non_local_base_url(knowledge_system: KnowledgeSystem) -> None:
    try:
        knowledge_system.weflow.check_connection(
            base_url="https://example.com",
            access_token="token",
        )
    except RuntimeError as exc:
        assert "只允许访问本机" in str(exc)
    else:
        raise AssertionError("non-local WeFlow URL should be rejected")


def test_chatlab_inspection_token_detects_file_change(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    chatlab = copy_fixture(source_root)
    inspection = knowledge_system.weflow.inspect_chatlab_file(str(chatlab))
    payload = json.loads(chatlab.read_text(encoding="utf-8"))
    payload["messages"].append(
        {
            "sender": "wxid_customer_demo",
            "timestamp": 1738714000,
            "type": 0,
            "content": "检查后新增的消息",
            "platformMessageId": "wf-demo-004",
        }
    )
    chatlab.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = knowledge_system.customer_workflows.import_chatlab(
        path=str(chatlab),
        inspection_token=inspection["inspection_token"],
        session_id=None,
        privacy="restricted",
    )
    assert result["status"] == "failed"
    assert "发生了变化" in result["error"]["message"]
