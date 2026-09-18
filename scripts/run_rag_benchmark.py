"""Run a repeatable, source-grounded retrieval benchmark for PKAS.

The benchmark follows the useful separation used by BEIR/RAG evaluation work:
the same fixed cases are replayed against lexical, vector, and hybrid retrieval.
Silver cases only claim source-location metrics; precision is deliberately left to
the human-graded gold set because the expected source list is not exhaustive.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.config import get_settings
from pkas.system import KnowledgeSystem


def _ndcg(returned: list[str], expected: set[str], top_k: int) -> float:
    """Binary nDCG against the known expected sources for a silver case."""

    def dcg(values: list[int]) -> float:
        import math

        return sum(
            value / math.log2(index + 2) for index, value in enumerate(values[:top_k])
        )

    actual = [int(source_id in expected) for source_id in returned]
    ideal = [1] * min(len(expected), top_k)
    denominator = dcg(ideal)
    return dcg(actual) / denominator if denominator else 0.0


def _score_case(
    returned: list[str], expected: set[str], top_k: int
) -> dict[str, float | int | None]:
    top = returned[:top_k]
    matched = len(set(top) & expected)
    ranks = [index + 1 for index, source_id in enumerate(top) if source_id in expected]
    rank = min(ranks) if ranks else None
    return {
        "hit": int(bool(ranks)),
        "located_recall": matched / len(expected) if expected else 0.0,
        "mrr": 1 / rank if rank else 0.0,
        "ndcg": _ndcg(top, expected, top_k),
        "first_rank": rank,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return round(ordered[index], 2)


def _select_cases(system: KnowledgeSystem) -> list[dict[str, Any]]:
    return [
        case
        for case in system.rag.list_cases()
        if case["review_status"] != "rejected" and case["expected_source_ids"]
    ]


def _stratified_cases(cases: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Choose a deterministic category/difficulty-balanced slice."""

    if limit <= 0 or limit >= len(cases):
        return cases
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for case in sorted(cases, key=lambda item: str(item["id"])):
        key = (str(case.get("category") or "general"), str(case.get("difficulty") or "normal"))
        buckets.setdefault(key, []).append(case)
    ordered = sorted(buckets)
    selected: list[dict[str, Any]] = []
    cursor = 0
    while len(selected) < limit and ordered:
        key = ordered[cursor % len(ordered)]
        bucket = buckets[key]
        if bucket:
            selected.append(bucket.pop(0))
        ordered = [item for item in ordered if buckets[item]]
        cursor += 1
    return selected


def _run_mode(
    system: KnowledgeSystem,
    cases: list[dict[str, Any]],
    *,
    mode: str,
    top_k: int,
    rerank_mode: str,
    source_statuses: dict[str, str],
    vector_source_ids: set[str],
) -> dict[str, Any]:
    totals = Counter()
    strata: dict[str, Counter[str]] = {}
    warnings = Counter()
    latencies: list[float] = []
    errors = 0
    expected_sources = 0
    indexed_expected_sources = 0
    vectorized_expected_sources = 0
    vector_eligible_cases = 0

    for case in cases:
        started = time.perf_counter()
        try:
            response = system.retrieval.search(
                case["query"],
                domain=case["domain"],
                limit=top_k,
                include_restricted=case["include_restricted"],
                include_unverified_claims=False,
                rerank_mode=rerank_mode,  # type: ignore[arg-type]
                expand_parent=False,
                retrieval_mode=mode,  # type: ignore[arg-type]
            )
            returned = [str(item.get("source_id") or "") for item in response.results]
            expected = {str(source_id) for source_id in case["expected_source_ids"]}
            expected_sources += len(expected)
            indexed_expected_sources += sum(
                source_statuses.get(source_id) == "indexed" for source_id in expected
            )
            vectorized_expected_sources += len(expected & vector_source_ids)
            vector_eligible_cases += int(bool(expected & vector_source_ids))
            score = _score_case(returned, expected, top_k)
            stratum = f"{case.get('category') or 'general'}:{case.get('difficulty') or 'normal'}"
            bucket = strata.setdefault(stratum, Counter())
            bucket.update(
                count=1,
                hit=int(score["hit"]),
                located_recall=float(score["located_recall"]),
                mrr=float(score["mrr"]),
                ndcg=float(score["ndcg"]),
            )
            totals.update(
                count=1,
                hit=int(score["hit"]),
                located_recall=float(score["located_recall"]),
                mrr=float(score["mrr"]),
                ndcg=float(score["ndcg"]),
            )
            warnings.update(str(warning) for warning in response.warnings)
        except Exception as exc:  # keep one failed mode from hiding other comparisons
            errors += 1
            warnings[f"mode_error:{type(exc).__name__}"] += 1
        finally:
            latencies.append((time.perf_counter() - started) * 1000)

    count = int(totals["count"])

    def average(name: str) -> float:
        return round(float(totals[name]) / count, 6) if count else 0.0

    return {
        "status": "completed" if errors == 0 else "warning",
        "retrieval_mode": mode,
        "rerank_mode": rerank_mode,
        "case_count": len(cases),
        "evaluated_count": count,
        "error_count": errors,
        "hit_rate_at_k": average("hit"),
        "source_located_recall_at_k": average("located_recall"),
        "mrr": average("mrr"),
        "ndcg_at_k": average("ndcg"),
        "corpus_coverage": {
            "expected_source_count": expected_sources,
            "indexed_expected_source_rate": round(
                indexed_expected_sources / expected_sources, 6
            )
            if expected_sources
            else 0.0,
            "vectorized_expected_source_rate": round(
                vectorized_expected_sources / expected_sources, 6
            )
            if expected_sources
            else 0.0,
            "cases_with_vectorized_expected_source": vector_eligible_cases,
            "case_vector_eligibility_rate": round(
                vector_eligible_cases / count, 6
            )
            if count
            else 0.0,
        },
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "warnings": dict(warnings),
        "strata": {
            name: {
                "case_count": int(values["count"]),
                "hit_rate_at_k": round(values["hit"] / values["count"], 6),
                "source_located_recall_at_k": round(
                    values["located_recall"] / values["count"], 6
                ),
                "mrr": round(values["mrr"] / values["count"], 6),
                "ndcg_at_k": round(values["ndcg"] / values["count"], 6),
            }
            for name, values in sorted(strata.items())
        },
        "precision_status": (
            "not_claimed_silver_expected_sources_not_exhaustive"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PKAS retrieval benchmark.")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--modes",
        default="a,b,ab",
        help="Comma-separated retrieval modes: a, b, ab.",
    )
    parser.add_argument(
        "--rerank-mode",
        choices=("never", "auto", "always"),
        default="never",
        help="Default is never to avoid extra rerank API calls during the baseline.",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 means all eligible cases.")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    settings = get_settings()
    system = KnowledgeSystem.create(settings)
    cases = _select_cases(system)
    total_eligible_cases = len(cases)
    cases = _stratified_cases(cases, args.limit)
    expected_ids = {
        str(source_id)
        for case in cases
        for source_id in case["expected_source_ids"]
    }
    with system.database.connect() as connection:
        source_statuses = {
            str(row["id"]): str(row["status"])
            for row in connection.execute(
                f"SELECT id, status FROM sources WHERE id IN "
                f"({','.join('?' for _ in expected_ids)})",
                sorted(expected_ids),
            ).fetchall()
        } if expected_ids else {}
        vector_source_ids = {
            str(row["source_id"])
            for row in connection.execute(
                """SELECT DISTINCT c.source_id FROM vector_index_state v
                JOIN chunks c ON c.id=v.chunk_id
                WHERE v.provider=? AND v.model=? AND v.collection_name=?""",
                (
                    system.rag.vector_index.embedding.provider_name,
                    system.rag.vector_index.embedding.model_name,
                    system.rag.vector_index.settings.qdrant_collection,
                ),
            ).fetchall()
        }
    modes = [item.strip() for item in args.modes.split(",") if item.strip()]
    invalid = sorted(set(modes) - {"a", "b", "ab"})
    if invalid:
        raise SystemExit(f"unsupported retrieval mode: {', '.join(invalid)}")

    results = {
        mode: _run_mode(
            system,
            cases,
            mode=mode,
            top_k=max(1, args.top_k),
            rerank_mode=args.rerank_mode,
            source_statuses=source_statuses,
            vector_source_ids=vector_source_ids,
        )
        for mode in modes
    }
    progress = system.rag.review_progress()
    report = {
        "status": "completed" if cases else "warning",
        "protocol": "pkas-rag-bench-v1",
        "method_family": ["BEIR-style fixed source labels", "RAGAS-style layer separation"],
        "created_at": datetime.now(UTC).isoformat(),
        "case_tier": "silver",
        "selection": {
            "eligible_case_count": total_eligible_cases,
            "evaluated_case_count": len(cases),
            "method": "deterministic category+difficulty stratification"
            if args.limit > 0 and args.limit < total_eligible_cases
            else "all eligible cases",
        },
        "case_count": len(cases),
        "top_k": max(1, args.top_k),
        "rerank_mode": args.rerank_mode,
        "modes": results,
        "gold_progress": {
            "total_cases": progress["total"],
            "human_eligible_cases": progress["eligible"],
            "draft_cases_excluded_from_precision": progress["draft"],
            "decision_completion": progress["decision_completion"],
            "gold_completion": progress["gold_completion"],
        },
        "metric_definitions": {
            "hit_rate_at_k": "前 K 条中是否至少出现一个已绑定来源。",
            "source_located_recall_at_k": (
                "已绑定来源中有多少出现在前 K 条；银标不声称来源集合穷尽。"
            ),
            "mrr": "第一个已绑定来源排名的倒数。",
            "ndcg_at_k": "按排名折扣的二值来源相关性。",
            "precision": "只在人工逐结果标注覆盖完整时计算，不用银标冒充。",
        },
        "next_step": (
            "补足人工金标并运行 gold 评测；再对最终回答增加 context precision/recall、"
            "faithfulness、answer relevancy。"
        ),
    }
    output = args.output or (settings.project_root / "reports" / "rag-benchmark-latest.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    public = dict(report)
    print(json.dumps(public, ensure_ascii=False))


if __name__ == "__main__":
    main()
