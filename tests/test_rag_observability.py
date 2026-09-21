from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from pkas.rag_observability import RagObservabilityService
from pkas.retrieval import RetrievalResponse
from pkas.system import KnowledgeSystem


class FixedRetrieval:
    def __init__(self, source_ids: list[str]) -> None:
        self.source_ids = source_ids

    def search(self, *_args: object, **_kwargs: object) -> RetrievalResponse:
        return RetrievalResponse(
            results=[{"source_id": source_id} for source_id in self.source_ids],
            mode="test",
        )


class RankedRetrieval:
    def search(self, *_args: object, **_kwargs: object) -> RetrievalResponse:
        return RetrievalResponse(
            results=[
                {
                    "source_id": "irrelevant",
                    "channel_ranks": {"fts": 2, "vector": 1},
                },
                {
                    "source_id": "relevant",
                    "channel_ranks": {"fts": 1, "vector": 2},
                },
            ],
            mode="test",
        )


def test_status_snapshot_returns_immediately_while_metrics_refresh(
    knowledge_system: KnowledgeSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()

    def delayed_collect() -> dict:
        started.set()
        assert release.wait(timeout=2)
        return {
            "provider": "test",
            "model": "test-model",
            "scope": "selected_l3",
            "collection": "test-collection",
            "eligible_chunks": 1,
            "indexed_chunks": 1,
            "pending_chunks": 0,
            "coverage": 1.0,
            "domains": {},
            "coverage_matrix": [],
            "coverage_scope": {},
            "retrieval_surfaces": {},
            "structure": {},
            "qdrant": {"status": "ready"},
            "mcp_enabled": False,
        }

    monkeypatch.setattr(knowledge_system.rag, "_collect_status", delayed_collect)
    began = monotonic()
    pending = knowledge_system.rag.status_snapshot()
    assert monotonic() - began < 0.2
    assert pending["snapshot_state"] == "refreshing"
    assert started.wait(timeout=1)
    release.set()

    deadline = monotonic() + 2
    current = pending
    while monotonic() < deadline:
        current = knowledge_system.rag.status_snapshot()
        if current["snapshot_state"] == "ready":
            break
        sleep(0.01)
    assert current["snapshot_state"] == "ready"
    assert current["qdrant"]["status"] == "ready"


def test_graded_judgments_drive_precision_ndcg_and_coverage(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    relevant_path = source_root / "relevant.md"
    irrelevant_path = source_root / "irrelevant.md"
    relevant_path.write_text("MCP 服务端向客户端提供知识检索工具。", encoding="utf-8")
    irrelevant_path.write_text("这是一份不相关的项目采购记录。", encoding="utf-8")
    relevant = knowledge_system.ingestion.import_file(
        relevant_path, domain="work", privacy="private"
    )
    irrelevant = knowledge_system.ingestion.import_file(
        irrelevant_path, domain="work", privacy="private"
    )
    knowledge_system.rag.retrieval = FixedRetrieval(  # type: ignore[assignment]
        [relevant["source_id"], irrelevant["source_id"]]
    )

    case = knowledge_system.rag.create_case(
        query="MCP 服务端给客户端提供什么？",
        expected_source_ids=[relevant["source_id"]],
        domain="work",
        include_restricted=False,
        tags=["mcp"],
        category="code",
        difficulty="normal",
        review_status="reviewed",
        judgment_scope_source_ids=[relevant["source_id"], irrelevant["source_id"]],
        judgments=[
            {
                "source_id": relevant["source_id"],
                "relevance_grade": 3,
                "judgment_basis": "human",
            },
            {
                "source_id": irrelevant["source_id"],
                "relevance_grade": 0,
                "judgment_basis": "human",
            }
        ],
    )
    knowledge_system.rag.create_case(
        query="草稿题不会进入正式测评吗？",
        expected_source_ids=[irrelevant["source_id"]],
        domain="work",
        include_restricted=False,
        tags=["draft"],
        review_status="draft",
    )

    cases = knowledge_system.rag.list_cases()
    saved = next(item for item in cases if item["id"] == case["id"])
    run = knowledge_system.rag.run_eval(top_k=2, rerank_mode="never")

    assert saved["category"] == "code"
    assert saved["review_eligible"] is True
    assert {item["relevance_grade"] for item in saved["judgments"]} == {0, 3}
    assert run["case_count"] == 1
    assert run["hit_rate"] == 1
    assert run["recall_at_k"] == 1
    assert run["precision_at_k"] == pytest.approx(0.5)
    assert run["judgment_coverage"] == 1
    assert run["ndcg_at_k"] == 1
    assert run["metric_status"] == "complete"
    assert run["eval_protocol"] == "graded-scope-v3-ab"
    assert run["strata"]["code:normal"]["case_count"] == 1

    updated = knowledge_system.rag.create_case(
        query="MCP 服务端给客户端提供什么？",
        expected_source_ids=[irrelevant["source_id"]],
        domain="work",
        include_restricted=False,
        tags=["review-correction"],
        review_status="draft",
        replace_expected=True,
    )
    corrected = next(
        item for item in knowledge_system.rag.list_cases() if item["id"] == updated["id"]
    )
    assert corrected["expected_source_ids"] == [irrelevant["source_id"]]
    assert corrected["review_status"] == "draft"


def test_ndcg_penalizes_relevant_source_rank() -> None:
    score = RagObservabilityService._ndcg([0, 3], [3], 2)

    assert score == pytest.approx(1 / 1.584962500721156)


def test_vector_status_explains_policy_exclusions(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    private = source_root / "private.md"
    restricted = source_root / "restricted.md"
    private.write_text("允许进入远程向量化的普通资料。", encoding="utf-8")
    restricted.write_text("默认只保留本地全文检索的受限资料。", encoding="utf-8")
    private_result = knowledge_system.ingestion.import_file(
        private,
        domain="work",
        privacy="private",
    )
    restricted_result = knowledge_system.ingestion.import_file(
        restricted,
        domain="work",
        privacy="restricted",
    )
    # Production vectors are intentionally limited to an explicit L3 intake
    # decision. Model that decision here so this test isolates the separate
    # restricted-data policy rather than accidentally testing the L3 gate.
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
            [(private_result["source_id"],), (restricted_result["source_id"],)],
        )
        connection.commit()

    status = knowledge_system.rag.status()

    assert status["coverage_scope"]["all_indexed_chunks"] == 2
    assert status["coverage_scope"]["vector_eligible_chunks"] == 1
    assert status["coverage_scope"]["excluded_restricted_policy"] == 1
    assert status["coverage_scope"]["restricted_embedding_enabled"] is False
    assert status["retrieval_surfaces"]["document_fts"] == {
        "records": 2,
        "indexed": 2,
        "physical_rows": 2,
        "engine": "SQLite FTS5",
        "semantic": False,
    }
    assert status["retrieval_surfaces"]["customer_message_fts"]["records"] == 0
    assert {item["privacy"] for item in status["coverage_matrix"]} == {
        "private",
        "restricted",
    }


def test_vector_map_balances_domain_and_source_type_buckets() -> None:
    records = [
        SimpleNamespace(id=f"work-{index}", payload={"domain": "work", "source_type": "md"})
        for index in range(10)
    ] + [
        SimpleNamespace(id="self-1", payload={"domain": "self", "source_type": "json"}),
        SimpleNamespace(id="work-html", payload={"domain": "work", "source_type": "html"}),
    ]

    selected = RagObservabilityService._stratified_records(records, 3)
    buckets = {
        (item.payload["domain"], item.payload["source_type"]) for item in selected
    }

    assert buckets == {("self", "json"), ("work", "html"), ("work", "md")}


def test_review_context_restores_missed_expected_source_and_requires_human_scope(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    expected_path = source_root / "expected.md"
    returned_path = source_root / "returned.md"
    expected_path.write_text("MCP 工具通过服务端声明。", encoding="utf-8")
    returned_path.write_text("这是当前检索返回的相邻资料。", encoding="utf-8")
    expected = knowledge_system.ingestion.import_file(
        expected_path, domain="work", privacy="private"
    )
    returned = knowledge_system.ingestion.import_file(
        returned_path, domain="work", privacy="private"
    )
    case = knowledge_system.rag.create_case(
        query="MCP 工具由哪里声明？",
        expected_source_ids=[expected["source_id"]],
        domain="work",
        include_restricted=False,
        tags=["review-queue"],
        review_status="draft",
    )
    knowledge_system.rag.retrieval = FixedRetrieval(  # type: ignore[assignment]
        [returned["source_id"]]
    )

    context = knowledge_system.rag.review_context(case["id"], rerank_mode="never")

    assert [item["source_id"] for item in context["candidates"]] == [
        returned["source_id"],
        expected["source_id"],
    ]
    assert context["candidates"][1]["retrieved"] is False
    assert context["candidates"][1]["seeded"] is True
    assert context["human_judged_count"] == 0

    reviewed = knowledge_system.rag.finalize_review(
        case["id"],
        judgments=[
            {
                "source_id": returned["source_id"],
                "relevance_grade": 0,
                "judgment_basis": "human",
            },
            {
                "source_id": expected["source_id"],
                "relevance_grade": 3,
                "judgment_basis": "human",
            },
        ],
        judgment_scope_source_ids=[returned["source_id"], expected["source_id"]],
        category="code",
        difficulty="hard",
        match_policy="any",
    )
    progress = knowledge_system.rag.review_progress()

    assert reviewed["review_eligible"] is True
    assert reviewed["expected_source_ids"] == [expected["source_id"]]
    assert {item["judgment_basis"] for item in reviewed["judgments"]} == {"human"}
    assert progress["eligible"] == 1
    assert progress["pending"] == 0


def test_reviewed_case_rejects_automatic_expected_source_grade(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "automatic.md"
    note.write_text("自动来源绑定不是人工金标。", encoding="utf-8")
    imported = knowledge_system.ingestion.import_file(
        note, domain="work", privacy="private"
    )

    with pytest.raises(ValueError, match="未经人工确认"):
        knowledge_system.rag.create_case(
            query="自动绑定可以直接成为金标吗？",
            expected_source_ids=[imported["source_id"]],
            domain="work",
            include_restricted=False,
            tags=["strict-human"],
            review_status="reviewed",
            judgment_scope_source_ids=[imported["source_id"]],
        )


def test_rejected_case_is_audited_but_not_forced_into_gold(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "ambiguous.md"
    note.write_text("这道题的描述存在歧义。", encoding="utf-8")
    imported = knowledge_system.ingestion.import_file(
        note, domain="work", privacy="private"
    )
    case = knowledge_system.rag.create_case(
        query="这里指的是什么？",
        expected_source_ids=[imported["source_id"]],
        domain="work",
        include_restricted=False,
        tags=["reject-test"],
        review_status="draft",
    )

    rejected = knowledge_system.rag.reject_case(case["id"], "ambiguous")
    progress = knowledge_system.rag.review_progress()

    assert rejected["review_status"] == "rejected"
    assert rejected["review_eligible"] is False
    assert "human-rejected-v1" in rejected["tags"]
    assert "rejection:ambiguous" in rejected["tags"]
    assert progress["pending"] == 0
    assert progress["rejected"] == 1
    assert progress["replacement_needed"] == 1
    assert progress["eligible"] == 0


def test_fusion_tuning_waits_for_human_gold(knowledge_system: KnowledgeSystem) -> None:
    status = knowledge_system.rag.fusion_tuning_status(minimum_gold_cases=1)

    assert status["status"] == "waiting_for_human_gold"
    assert status["eligible_gold_cases"] == 0
    assert status["may_apply"] is False
    assert status["current_weights"] == {"fts": 1.0, "vector": 1.0}

    tuning = knowledge_system.rag.tune_fusion_weights(minimum_gold_cases=1)
    assert tuning["status"] == "waiting_for_human_gold"
    assert tuning["recommendation"] is None


def test_fusion_tuning_replays_channel_ranks_without_rerank(
    knowledge_system: KnowledgeSystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [
        {
            "id": f"case-{index}",
            "query": "相关资料在哪里？",
            "domain": "work",
            "include_restricted": False,
            "review_eligible": True,
            "judgments": [
                {
                    "source_id": "relevant",
                    "relevance_grade": 3,
                    "judgment_basis": "human",
                },
                {
                    "source_id": "irrelevant",
                    "relevance_grade": 0,
                    "judgment_basis": "human",
                },
            ],
        }
        for index in range(5)
    ]
    knowledge_system.rag.retrieval = RankedRetrieval()  # type: ignore[assignment]
    monkeypatch.setattr(knowledge_system.rag, "list_cases", lambda: cases)

    tuning = knowledge_system.rag.tune_fusion_weights(minimum_gold_cases=5, top_k=1)

    assert tuning["status"] == "completed"
    assert tuning["applied"] is False
    assert tuning["recommendation"]["weights"]["fts"] > 1.0
