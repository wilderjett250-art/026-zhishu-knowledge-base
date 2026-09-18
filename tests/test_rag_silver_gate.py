import json
from pathlib import Path

import pytest
from scripts.run_rag_silver_gate import (
    _diagnose_case,
    _load_gate,
    _select_silver_cases,
)


def test_silver_gate_config_requires_complete_protocol(tmp_path: Path) -> None:
    config = tmp_path / "gate.json"
    config.write_text(json.dumps({"protocol": "test"}), encoding="utf-8")

    with pytest.raises(ValueError, match="expected_case_count"):
        _load_gate(config)


def test_repository_silver_gate_config_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    gate = _load_gate(root / "config" / "rag_silver_gate.json")

    assert gate["protocol"] == "source-located-silver-v1"
    assert gate["expected_case_count"] == 150
    assert gate["minimum_hit_rate"] >= 0.95
    assert gate["diagnostic_depth"] > gate["top_k"]


@pytest.mark.parametrize(
    ("baseline", "final", "rerank_status", "vectorized", "expected_status", "reason"),
    [
        (["other"], ["expected"], "completed", True, "indexed", "rerank_rescue"),
        (["expected"], ["other"], "completed", True, "indexed", "rerank_drop"),
        (["other", "expected"], ["other", "expected"], "skipped", True, "indexed", "ranking_miss"),
        (["other"], ["other"], "skipped", True, "indexed", "candidate_miss"),
        (["other"], ["other"], "skipped", False, "indexed", "candidate_miss_no_vector"),
        (["other"], ["other"], "skipped", False, "failed", "source_unavailable"),
    ],
)
def test_silver_diagnosis_explains_retrieval_failure_stage(
    baseline: list[str],
    final: list[str],
    rerank_status: str,
    vectorized: bool,
    expected_status: str,
    reason: str,
) -> None:
    result = _diagnose_case(
        expected={"expected"},
        source_statuses={"expected": expected_status},
        vector_source_ids={"expected"} if vectorized else set(),
        baseline_returned=baseline,
        final_returned=final,
        top_k=1,
        rerank_status=rerank_status,
    )

    assert result["diagnosis"] == reason


def test_silver_case_set_survives_human_promotion_but_excludes_rejections() -> None:
    cases = [
        {
            "id": "draft",
            "review_status": "draft",
            "review_eligible": False,
            "expected_source_ids": ["source-1"],
        },
        {
            "id": "human-gold",
            "review_status": "reviewed",
            "review_eligible": True,
            "expected_source_ids": ["source-2"],
        },
        {
            "id": "rejected",
            "review_status": "rejected",
            "review_eligible": False,
            "expected_source_ids": ["source-3"],
        },
    ]

    assert [case["id"] for case in _select_silver_cases(cases)] == [
        "draft",
        "human-gold",
    ]
