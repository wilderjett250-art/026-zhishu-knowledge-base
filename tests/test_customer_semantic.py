from pkas.search_gateway import UnifiedSearchRequest, unified_search
from pkas.vector_index import VectorSearchResult


def test_chat_summary_candidates_are_restricted_and_source_specific(knowledge_system):
    stored = knowledge_system.ingestion.import_text(
        text="微信会话语义摘要\n明确主题：设备需求\n明确待办：确认交付时间",
        title="微信会话摘要 test-conversation",
        original_uri="wechat-summary://test-conversation/wechat-summary-v1",
        source_type="wechat-summary",
        domain="work",
        privacy="restricted",
        metadata={
            "requested_processing_level": "L3",
            "derived_kind": "wechat_conversation_summary",
        },
    )
    with knowledge_system.database.connect() as connection:
        connection.execute(
            """
            UPDATE sources
            SET metadata_json = json_set(
                metadata_json, '$.requested_processing_level', 'L3'
            )
            WHERE id = ?
            """,
            (stored["source_id"],),
        )
        connection.commit()

    candidates = knowledge_system.customer_semantic.vector_index._candidates()
    assert len(candidates) == 1
    assert candidates[0]["source_type"] == "wechat-summary"
    assert candidates[0]["privacy"] == "restricted"


def test_chat_search_combines_local_fts_and_summary_semantic(monkeypatch, knowledge_system):
    monkeypatch.setattr(
        knowledge_system.customers,
        "search_messages",
        lambda *args, **kwargs: [{"message_id": "m1", "match_strategy": "fts"}],
    )
    monkeypatch.setattr(
        knowledge_system.customer_semantic,
        "search",
        lambda *args, **kwargs: VectorSearchResult(
            [{"chunk_id": "c1", "match_strategy": "vector_semantic"}], "ready"
        ),
    )

    result = unified_search(
        knowledge_system,
        UnifiedSearchRequest(
            query="设备需求",
            scopes=["chats"],
            include_restricted=True,
            rerank_mode="never",
        ),
    )

    assert result["groups"]["chats"]["mode"] == "chat_hybrid"
    assert len(result["groups"]["chats"]["results"]) == 1
    assert len(result["groups"]["chats"]["semantic_results"]) == 1
    assert len(result["results"]) == 2


def test_fallback_chat_summary_is_explicitly_uncertain(knowledge_system):
    packet = {
        "conversation_id": "conversation-1",
        "message_count": 2,
        "messages": [{"speaker": "对方", "text": "确认设备需求"}],
    }
    item = knowledge_system.customer_semantic._fallback_summary(packet)
    assert item["fallback"] is True
    assert "不能视为完整总结" in item["summary"]
