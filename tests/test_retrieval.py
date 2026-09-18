from collections.abc import Sequence

from qdrant_client import QdrantClient

from pkas.local_secrets import load_user_secret, save_user_secret
from pkas.reranker import RerankResponse
from pkas.retrieval import RetrievalService
from pkas.system import KnowledgeSystem
from pkas.vector_index import QdrantVectorIndex, _embedding_text


def test_embedding_text_includes_bounded_source_path_for_same_named_code_files() -> None:
    text = _embedding_text(
        {
            "title": "index.vue",
            "text_content": "fetchSkillList()",
            "original_uri": r"E:\repo\src\views\skills\index.vue",
        }
    )

    assert "Source path: repo/src/views/skills/index.vue" in text
    assert "fetchSkillList()" in text


class FakeEmbeddingProvider:
    provider_name = "fake"
    model_name = "fake-embedding-v1"
    enabled = True

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            normalized = text.lower()
            if "苹果" in normalized or "iphone" in normalized or "手机" in normalized:
                vectors.append([1.0, 0.0, 0.0])
            elif "数据库" in normalized or "sqlite" in normalized:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return vectors


class FakeReranker:
    model_name = "fake-reranker-v1"
    enabled = True

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> RerankResponse:
        _ = query
        order = list(reversed(range(len(documents))))[:top_n]
        return RerankResponse(
            scores=[(index, 1.0 - rank * 0.01) for rank, index in enumerate(order)],
            usage={"test": True},
        )


class RecordingReranker(FakeReranker):
    def __init__(self) -> None:
        self.documents: list[str] = []

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> RerankResponse:
        self.documents = list(documents)
        return super().rerank(query, documents, top_n)


class FakeMachineCatalog:
    def search(self, query: str, limit: int):
        _ = (query, limit)
        return [
            {
                "source_id": "catalog:1",
                "title": "项目目录",
                "snippet": "alpha-project",
                "source_type": "filesystem-catalog",
                "retrieval_channels": ["catalog"],
                "domain": "local",
                "privacy": "private",
                "original_uri": "D:/alpha-project",
            }
        ]


def _mark_l3(knowledge_system: KnowledgeSystem, *source_ids: str) -> None:
    """Model the explicit L3 intake decision required by the production index."""
    with knowledge_system.database.connect() as connection:
        connection.executemany(
            """
            UPDATE sources
            SET metadata_json = json_set(
                COALESCE(metadata_json, '{}'),
                '$.requested_processing_level', 'L3'
            )
            WHERE id = ?
            """,
            [(source_id,) for source_id in source_ids],
        )
        connection.commit()


def test_explicit_a_b_ab_modes_keep_catalog_and_vector_separate(
    knowledge_system: KnowledgeSystem,
) -> None:
    imported = knowledge_system.ingestion.import_text(
        text="苹果手机项目的维修流程。",
        title="维修知识",
        original_uri="memory://repair",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    _mark_l3(knowledge_system, imported["source_id"])
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    vector_index._client = QdrantClient(location=":memory:")
    vector_index.sync()
    retrieval = RetrievalService(
        knowledge_system.repository, vector_index, machine_catalog=FakeMachineCatalog()
    )

    a = retrieval.search("苹果手机项目", retrieval_mode="a", rerank_mode="never")
    b = retrieval.search("苹果手机项目", retrieval_mode="b", rerank_mode="never")
    ab = retrieval.search("苹果手机项目", retrieval_mode="ab", rerank_mode="never")

    assert a.mode == "a_lexical_catalog"
    assert {channel for item in a.results for channel in item["retrieval_channels"]} <= {
        "fts",
        "catalog",
    }
    assert b.mode == "b_vector"
    assert {channel for item in b.results for channel in item["retrieval_channels"]} == {"vector"}
    assert ab.mode == "hybrid"
    assert {channel for item in ab.results for channel in item["retrieval_channels"]} >= {
        "fts",
        "vector",
    }


def test_vector_sync_is_incremental_and_semantic_search_keeps_provenance(
    knowledge_system: KnowledgeSystem,
) -> None:
    imported = knowledge_system.ingestion.import_text(
        text="苹果设备售后需要先核对序列号和保修状态。",
        title="设备售后流程",
        original_uri="memory://device-support",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    _mark_l3(knowledge_system, imported["source_id"])
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    vector_index._client = QdrantClient(location=":memory:")
    retrieval = RetrievalService(knowledge_system.repository, vector_index)

    first = vector_index.sync()
    second = vector_index.sync()
    response = retrieval.search("iPhone 出问题后应该先做什么", domain="work", limit=5)

    assert first["status"] == "completed"
    assert first["indexed"] >= 1
    assert second == {
        "status": "completed",
        "indexed": 0,
        "unchanged": first["indexed"],
        "removed": 0,
        "pending": 0,
    }
    assert response.mode == "hybrid"
    assert response.results[0]["source_id"] == imported["source_id"]
    assert response.results[0]["original_uri"] == "memory://device-support"
    assert "vector" in response.results[0]["retrieval_channels"]
    assert (
        response.results[0]["parent_context"]["anchor_chunk_id"] == response.results[0]["chunk_id"]
    )

    points, _ = vector_index._client.scroll(
        collection_name=knowledge_system.settings.qdrant_collection,
        with_payload=True,
        with_vectors=False,
    )
    assert points
    assert "text_content" not in points[0].payload
    assert "original_uri" not in points[0].payload

    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE sources SET status = 'superseded' WHERE id = ?",
            (imported["source_id"],),
        )
        connection.commit()
    cleanup = vector_index.sync()
    semantic_after_cleanup = vector_index.search(
        "iPhone 故障",
        domain="work",
        limit=5,
        include_restricted=False,
    )
    assert cleanup["removed"] >= 1
    assert semantic_after_cleanup.items == []


def test_vector_coverage_and_search_exclude_unreviewed_thread_summaries(
    knowledge_system: KnowledgeSystem,
) -> None:
    formal = knowledge_system.ingestion.import_text(
        text="正式项目资料，包含可搜索的交付规范。",
        title="正式资料",
        original_uri="memory://formal",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    draft = knowledge_system.ingestion.import_text(
        text="自动会话摘要，不能作为正式事实。",
        title="自动摘要",
        original_uri="memory://draft",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    _mark_l3(knowledge_system, formal["source_id"], draft["source_id"])
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    vector_index._client = QdrantClient(location=":memory:")
    vector_index.sync()
    with knowledge_system.database.connect() as connection:
        connection.execute(
            "UPDATE sources SET source_type='thread-summary' WHERE id=?",
            (draft["source_id"],),
        )
        connection.commit()

    coverage = vector_index.coverage()
    response = vector_index.search("自动会话摘要", domain="work", limit=5, include_restricted=False)

    assert coverage == {"eligible": 1, "indexed": 1, "pending": 0, "coverage": 1.0}
    assert [item["source_id"] for item in vector_index._candidates()] == [formal["source_id"]]
    assert all(item["source_type"] != "thread-summary" for item in response.items)


def test_vector_runtime_status_reuses_an_injected_local_client(
    knowledge_system: KnowledgeSystem,
) -> None:
    imported = knowledge_system.ingestion.import_text(
        text="运行状态探针测试资料",
        title="探针资料",
        original_uri="memory://runtime-probe",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    _mark_l3(knowledge_system, imported["source_id"])
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    vector_index._client = QdrantClient(location=":memory:")
    vector_index.sync()

    status = vector_index.runtime_status()

    assert status["status"] == "ready"
    assert status["points"] >= 1
    assert status["dimension"] == 3


def test_disabled_embedding_is_silent_fts_fallback(
    knowledge_system: KnowledgeSystem,
) -> None:
    knowledge_system.ingestion.import_text(
        text="本地全文检索仍然可用。",
        title="全文检索",
        original_uri="memory://fts-only",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )

    response = knowledge_system.retrieval.search("全文检索", domain="work")

    assert response.mode == "fts_only"
    assert response.warnings == []
    assert response.results


def test_vector_coverage_distinguishes_no_l3_scope_from_full_coverage(
    knowledge_system,
):
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )

    assert vector_index.coverage() == {
        "eligible": 0,
        "indexed": 0,
        "pending": 0,
        "coverage": None,
    }


def test_hybrid_search_does_not_open_vector_store_without_l3_candidates(
    knowledge_system: KnowledgeSystem,
    monkeypatch,
) -> None:
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("没有 L3 候选时不应启动向量检索")

    monkeypatch.setattr(vector_index, "search", fail_if_called)
    retrieval = RetrievalService(knowledge_system.repository, vector_index)

    response = retrieval.search("没有语义资料的问题", retrieval_mode="ab", rerank_mode="never")

    assert response.mode == "fts_only"


def test_explicit_rerank_reorders_candidates_and_reports_health(
    knowledge_system: KnowledgeSystem,
) -> None:
    imported_sources = []
    for index in range(3):
        imported_sources.append(knowledge_system.ingestion.import_text(
            text=f"统一检索测试资料，第 {index} 份。",
            title=f"候选资料 {index}",
            original_uri=f"memory://rerank-{index}",
            source_type="manual-note",
            domain="work",
            privacy="private",
        ))
    _mark_l3(knowledge_system, *(item["source_id"] for item in imported_sources))
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    vector_index._client = QdrantClient(location=":memory:")
    vector_index.sync()
    retrieval = RetrievalService(
        knowledge_system.repository,
        vector_index,
        FakeReranker(),
    )

    response = retrieval.search(
        "统一检索测试资料",
        domain="work",
        limit=3,
        rerank_mode="always",
    )

    assert response.mode == "hybrid_rerank"
    assert response.health is not None
    assert response.health.rerank_status == "applied"
    assert response.health.rerank_candidate_count == 3
    assert 0 < response.health.rerank_input_chars <= 60_000
    assert response.health.fusion_weights == {"fts": 1.0, "catalog": 1.0, "vector": 1.0}
    assert response.health.source_count == 3
    assert "rerank" in response.results[0]["retrieval_channels"]
    assert response.results[0]["channel_ranks"]
    assert response.results[0]["rerank_score"] == 1.0
    assert response.results[0]["rerank_rank"] == 1
    assert response.results[0]["rerank_fusion_score"] > 0


def test_complex_rerank_expands_candidates_but_respects_configured_depth(
    knowledge_system: KnowledgeSystem,
) -> None:
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    retrieval = RetrievalService(
        knowledge_system.repository,
        vector_index,
        FakeReranker(),
    )

    assert retrieval._rerank_limit("普通问题", 100) == 20
    assert retrieval._rerank_limit("综合比较多个项目的技术差异，并结合所有资料给出选择", 100) == 40


def test_query_plan_is_local_explainable_and_adapts_candidate_depth(
    knowledge_system: KnowledgeSystem,
) -> None:
    retrieval = knowledge_system.retrieval

    precise = retrieval._plan_query(r"找 E:\project\src\api.py 的接口配置", 5)
    synthesis = retrieval._plan_query("综合比较多个项目的技术差异，怎么选", 5)
    exploration = retrieval._plan_query("我的资料里有哪些嵌入式项目", 5)

    assert precise.intent == "precise_lookup"
    assert precise.candidate_limit == 48
    assert precise.prefer_source_diversity is False
    assert synthesis.intent == "cross_source_synthesis"
    assert synthesis.candidate_limit == 100
    assert synthesis.prefer_source_diversity is True
    assert exploration.intent == "exploration"
    assert exploration.candidate_limit == 80
    assert all(item.reasons for item in (precise, synthesis, exploration))


def test_search_reports_query_plan_to_shared_console_and_mcp_contract(
    knowledge_system: KnowledgeSystem,
) -> None:
    knowledge_system.ingestion.import_text(
        text="项目部署需要先确认服务地址。",
        title="部署说明",
        original_uri="memory://deployment",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )

    response = knowledge_system.retrieval.search("比较多个项目如何选择", rerank_mode="never")

    assert response.health is not None
    assert response.health.query_plan == {
        "intent": "cross_source_synthesis",
        "candidate_limit": 100,
        "prefer_source_diversity": True,
        "reasons": ["检测到跨资料比较或综合意图，扩大候选并按来源折叠。"],
    }


def test_rerank_documents_include_bounded_path_but_not_absolute_root(
    knowledge_system: KnowledgeSystem,
) -> None:
    primary = knowledge_system.ingestion.import_text(
        text="function handleViewSlices(docId) { sliceFilterDocId.value = docId; }",
        title="detail.vue",
        original_uri=r"E:\private-root\repo\src\views\rag\detail.vue",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    secondary = knowledge_system.ingestion.import_text(
        text="function unrelatedDashboard() { return 'other candidate'; }",
        title="dashboard.vue",
        original_uri=r"E:\another-root\repo\src\views\home\dashboard.vue",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    _mark_l3(knowledge_system, primary["source_id"], secondary["source_id"])
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    vector_index._client = QdrantClient(location=":memory:")
    vector_index.sync()
    reranker = RecordingReranker()
    retrieval = RetrievalService(knowledge_system.repository, vector_index, reranker)

    retrieval.search("detail view slices", limit=2, rerank_mode="always")

    assert reranker.documents
    assert any("Source path: repo/src/views/rag/detail.vue" in item for item in reranker.documents)
    assert all("private-root" not in item for item in reranker.documents)


def test_vector_candidates_require_explicit_l3_intake_decision(
    knowledge_system: KnowledgeSystem,
) -> None:
    l2 = knowledge_system.ingestion.import_text(
        text="只允许全文检索的资料。",
        title="L2 资料",
        original_uri="memory://l2-only",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    l3 = knowledge_system.ingestion.import_text(
        text="允许语义检索的资料。",
        title="L3 资料",
        original_uri="memory://l3-semantic",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )
    _mark_l3(knowledge_system, l3["source_id"])
    knowledge_system.settings.embedding_scope = "selected_l3"
    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )
    vector_index._client = QdrantClient(location=":memory:")

    result = vector_index.sync()

    assert result["indexed"] >= 1
    assert {item["source_id"] for item in vector_index._candidates()} == {l3["source_id"]}
    assert vector_index.coverage() == {
        "eligible": 1,
        "indexed": 1,
        "pending": 0,
        "coverage": 1.0,
    }
    assert l2["source_id"] not in {
        item["source_id"]
        for item in vector_index.search(
            "资料", domain="work", limit=5, include_restricted=False
        ).items
    }


def test_vector_candidates_default_to_selected_l3_material(
    knowledge_system: KnowledgeSystem,
) -> None:
    knowledge_system.ingestion.import_text(
        text="默认全量向量化范围测试。",
        title="默认全量范围",
        original_uri="memory://all-formal-scope",
        source_type="manual-note",
        domain="work",
        privacy="private",
    )

    vector_index = QdrantVectorIndex(
        knowledge_system.settings,
        knowledge_system.database,
        knowledge_system.repository,
        FakeEmbeddingProvider(),
    )

    assert knowledge_system.settings.embedding_scope == "selected_l3"
    assert vector_index._candidates() == []


def test_no_result_is_explicitly_insufficient(knowledge_system: KnowledgeSystem) -> None:
    response = knowledge_system.retrieval.search("完全不存在的知识答案")

    assert response.health is not None
    assert response.health.sufficiency == "insufficient"
    assert response.warnings == ["没有找到允许访问且可定位的证据。"]


def test_embedding_key_is_dpapi_encrypted_for_current_windows_user(
    knowledge_system: KnowledgeSystem,
) -> None:
    secret = "test-only-siliconflow-key-1234567890"

    path = save_user_secret(knowledge_system.settings, "embedding_api_key", secret)

    assert path.read_bytes() != secret.encode()
    assert load_user_secret(knowledge_system.settings, "embedding_api_key") == secret
