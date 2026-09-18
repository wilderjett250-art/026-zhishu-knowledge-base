from scripts.run_rag_benchmark import _ndcg, _score_case, _stratified_cases


def test_score_case_reports_source_located_metrics() -> None:
    score = _score_case(["noise", "expected", "other"], {"expected"}, 2)

    assert score["hit"] == 1
    assert score["located_recall"] == 1.0
    assert score["mrr"] == 0.5
    assert 0 < score["ndcg"] < 1


def test_ndcg_rewards_expected_source_at_rank_one() -> None:
    assert _ndcg(["expected", "noise"], {"expected"}, 2) == 1.0
    assert _ndcg(["noise", "expected"], {"expected"}, 2) < 1.0


def test_stratified_cases_are_deterministic_and_cover_buckets() -> None:
    cases = [
        {"id": "b", "category": "code", "difficulty": "hard"},
        {"id": "a", "category": "project", "difficulty": "normal"},
        {"id": "c", "category": "code", "difficulty": "normal"},
        {"id": "d", "category": "project", "difficulty": "normal"},
    ]

    selected = _stratified_cases(cases, 2)

    assert [case["id"] for case in selected] == ["b", "a"]
