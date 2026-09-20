"""Measure draft question source recall without promoting drafts to official gold data."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pkas.config import get_settings
from pkas.system import KnowledgeSystem


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rerank-mode", choices=("auto", "never", "always"), default="never")
    args = parser.parse_args()

    system = KnowledgeSystem.create(get_settings())
    system.database.initialize()
    cases = [case for case in system.rag.list_cases() if case["review_status"] == "draft"]
    details = []
    mode_counts: Counter[str] = Counter()
    strata: dict[str, Counter[str]] = {}
    reciprocal_rank_total = 0.0
    for case in cases:
        response = system.retrieval.search(
            case["query"],
            domain=case["domain"],
            limit=args.top_k,
            include_restricted=case["include_restricted"],
            include_unverified_claims=False,
            rerank_mode=args.rerank_mode,
            expand_parent=False,
        )
        returned = [str(item.get("source_id") or "") for item in response.results]
        expected = set(case["expected_source_ids"])
        ranks = [index + 1 for index, source_id in enumerate(returned) if source_id in expected]
        rank = min(ranks) if ranks else None
        reciprocal_rank_total += 1 / rank if rank else 0.0
        stratum = f"{case['category']}:{case['difficulty']}"
        strata.setdefault(stratum, Counter()).update(
            count=1,
            hit=int(rank is not None),
            reciprocal_rank=(1 / rank if rank else 0.0),
        )
        mode_counts[response.mode] += 1
        details.append(
            {
                "case_id": case["id"],
                "query": case["query"],
                "category": case["category"],
                "difficulty": case["difficulty"],
                "hit": rank is not None,
                "rank": rank,
                "returned_source_ids": returned,
                "warnings": response.warnings,
            }
        )
    count = len(details)
    hit_count = sum(int(item["hit"]) for item in details)
    report = {
        "status": "completed",
        "scope": "draft-only-source-recall",
        "rerank_mode": args.rerank_mode,
        "top_k": args.top_k,
        "case_count": count,
        "hit_count": hit_count,
        "hit_rate": hit_count / count if count else 0.0,
        "mrr": reciprocal_rank_total / count if count else 0.0,
        "retrieval_modes": dict(mode_counts),
        "miss_count": count - hit_count,
        "strata": {
            name: {
                "case_count": int(values["count"]),
                "hit_rate": values["hit"] / values["count"],
                "mrr": values["reciprocal_rank"] / values["count"],
            }
            for name, values in sorted(strata.items())
        },
        "details": details,
        "note": "草稿审计只测来源召回，不代表人工复核通过，也不计算 Precision。",
    }
    report_dir = Path(get_settings().project_root) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report_path = report_dir / f"rag-draft-audit-{args.rerank_mode}-{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "details"}
            | {"report_path": str(report_path)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
