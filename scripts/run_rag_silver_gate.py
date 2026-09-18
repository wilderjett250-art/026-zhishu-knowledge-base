"""Run the persistent source-located silver RAG regression gate."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.config import get_settings
from pkas.system import KnowledgeSystem


def _first_expected_rank(returned: list[str], expected: set[str]) -> int | None:
    ranks = [index + 1 for index, source_id in enumerate(returned) if source_id in expected]
    return min(ranks) if ranks else None


def _diagnose_case(
    *,
    expected: set[str],
    source_statuses: dict[str, str],
    vector_source_ids: set[str],
    baseline_returned: list[str],
    final_returned: list[str],
    top_k: int,
    rerank_status: str,
) -> dict[str, Any]:
    indexed_expected = {
        source_id for source_id in expected if source_statuses.get(source_id) == "indexed"
    }
    baseline_rank = _first_expected_rank(baseline_returned, expected)
    final_rank = _first_expected_rank(final_returned, expected)
    rerank_applied = rerank_status in {"completed", "applied", "success"}
    if not indexed_expected:
        diagnosis = "source_unavailable"
    elif final_rank is not None and final_rank <= top_k:
        diagnosis = (
            "rerank_rescue"
            if rerank_applied and (baseline_rank is None or baseline_rank > top_k)
            else "passed"
        )
    elif baseline_rank is not None and baseline_rank <= top_k:
        diagnosis = "rerank_drop" if rerank_applied else "ranking_instability"
    elif final_rank is not None or baseline_rank is not None:
        diagnosis = "ranking_miss"
    elif not (indexed_expected & vector_source_ids):
        diagnosis = "candidate_miss_no_vector"
    else:
        diagnosis = "candidate_miss"
    return {
        "diagnosis": diagnosis,
        "baseline_rank": baseline_rank,
        "final_rank": final_rank,
        "expected_source_available": bool(indexed_expected),
        "expected_source_vectorized": bool(indexed_expected & vector_source_ids),
    }


def _select_silver_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep stable source-located cases; human promotion must not remove a silver case."""
    return [
        case
        for case in cases
        if case["review_status"] != "rejected" and case["expected_source_ids"]
    ]


def _load_gate(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "protocol",
        "expected_case_count",
        "top_k",
        "rerank_mode",
        "minimum_hit_rate",
        "minimum_mrr",
        "required_categories",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"silver gate config missing: {', '.join(missing)}")
    return value


def run_gate(config_path: Path) -> tuple[dict[str, Any], Path]:
    gate = _load_gate(config_path)
    settings = get_settings()
    system = KnowledgeSystem.create(settings)
    system.database.initialize()
    cases = _select_silver_cases(system.rag.list_cases())
    all_expected_ids = {
        str(source_id)
        for case in cases
        for source_id in case["expected_source_ids"]
    }
    with system.database.connect() as connection:
        source_statuses = {
            str(row["id"]): str(row["status"])
            for row in connection.execute(
                f"SELECT id, status FROM sources WHERE id IN "
                f"({','.join('?' for _ in all_expected_ids)})",
                sorted(all_expected_ids),
            ).fetchall()
        } if all_expected_ids else {}
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
    details: list[dict[str, Any]] = []
    modes: Counter[str] = Counter()
    strata: dict[str, Counter[str]] = {}
    diagnoses: Counter[str] = Counter()
    degraded_warnings: Counter[str] = Counter()
    reciprocal_rank_total = 0.0
    diagnostic_depth = max(int(gate["top_k"]), int(gate.get("diagnostic_depth", 20)))
    for case in cases:
        response = system.retrieval.search(
            case["query"],
            domain=case["domain"],
            limit=int(gate["top_k"]),
            include_restricted=case["include_restricted"],
            include_unverified_claims=False,
            rerank_mode=str(gate["rerank_mode"]),  # type: ignore[arg-type]
            expand_parent=False,
        )
        baseline = system.retrieval.search(
            case["query"],
            domain=case["domain"],
            limit=diagnostic_depth,
            include_restricted=case["include_restricted"],
            include_unverified_claims=False,
            rerank_mode="never",
            expand_parent=False,
        )
        full_returned = [str(item.get("source_id") or "") for item in response.results]
        baseline_returned = [
            str(item.get("source_id") or "") for item in baseline.results
        ]
        returned = full_returned
        expected = set(case["expected_source_ids"])
        rerank_status = (
            str(response.health.rerank_status) if response.health else "unknown"
        )
        diagnostic = _diagnose_case(
            expected=expected,
            source_statuses=source_statuses,
            vector_source_ids=vector_source_ids,
            baseline_returned=baseline_returned,
            final_returned=full_returned,
            top_k=int(gate["top_k"]),
            rerank_status=rerank_status,
        )
        rank = (
            diagnostic["final_rank"]
            if diagnostic["final_rank"] is not None
            and diagnostic["final_rank"] <= int(gate["top_k"])
            else None
        )
        reciprocal_rank = 1 / rank if rank else 0.0
        reciprocal_rank_total += reciprocal_rank
        stratum = f"{case['category']}:{case['difficulty']}"
        strata.setdefault(stratum, Counter()).update(
            count=1,
            hit=int(rank is not None),
            reciprocal_rank=reciprocal_rank,
        )
        modes[response.mode] += 1
        diagnoses[str(diagnostic["diagnosis"])] += 1
        for warning in response.warnings:
            degraded_warnings[str(warning)] += 1
        details.append(
            {
                "case_id": case["id"],
                "category": case["category"],
                "difficulty": case["difficulty"],
                "hit": rank is not None,
                "rank": rank,
                "returned_source_ids": returned,
                "diagnostic_depth": diagnostic_depth,
                "baseline_returned_source_ids": baseline_returned,
                **diagnostic,
                "rerank_status": rerank_status,
                "warning_codes": response.warnings,
            }
        )
    count = len(details)
    hits = sum(int(item["hit"]) for item in details)
    hit_rate = hits / count if count else 0.0
    mrr = reciprocal_rank_total / count if count else 0.0
    categories = Counter(case["category"] for case in cases)
    failures = []
    if count != int(gate["expected_case_count"]):
        failures.append("case_count_mismatch")
    if hit_rate < float(gate["minimum_hit_rate"]):
        failures.append("hit_rate_below_minimum")
    if mrr < float(gate["minimum_mrr"]):
        failures.append("mrr_below_minimum")
    missing_categories = sorted(set(gate["required_categories"]) - set(categories))
    if missing_categories:
        failures.append("required_categories_missing")
    report = {
        "status": "passed" if not failures else "failed",
        "protocol": gate["protocol"],
        "case_tier": "silver",
        "case_count": count,
        "top_k": int(gate["top_k"]),
        "diagnostic_depth": diagnostic_depth,
        "rerank_mode": gate["rerank_mode"],
        "hit_count": hits,
        "hit_rate": hit_rate,
        "mrr": mrr,
        "miss_count": count - hits,
        "categories": dict(sorted(categories.items())),
        "missing_categories": missing_categories,
        "retrieval_modes": dict(modes),
        "diagnostics": dict(sorted(diagnoses.items())),
        "degraded_warnings": dict(sorted(degraded_warnings.items())),
        "thresholds": {
            "minimum_hit_rate": gate["minimum_hit_rate"],
            "minimum_mrr": gate["minimum_mrr"],
        },
        "failures": failures,
        "strata": {
            name: {
                "case_count": int(values["count"]),
                "hit_rate": values["hit"] / values["count"],
                "mrr": values["reciprocal_rank"] / values["count"],
            }
            for name, values in sorted(strata.items())
        },
        "details": details,
        "note": gate.get("note"),
    }
    report_dir = settings.project_root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"rag-silver-gate-{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report, report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(get_settings().project_root) / "config" / "rag_silver_gate.json",
    )
    args = parser.parse_args()
    report, report_path = run_gate(args.config)
    public = {key: value for key, value in report.items() if key != "details"}
    print(json.dumps(public | {"report_path": str(report_path)}, ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
