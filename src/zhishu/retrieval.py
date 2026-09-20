"""Shared A/B/AB retrieval contracts and deterministic result fusion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

RetrievalMode = Literal["a", "b", "ab"]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A traceable result returned by one retrieval channel."""

    source_id: str
    title: str
    locator: str
    channel: Literal["catalog", "fts", "vector"]
    score: float = 0.0
    channels: tuple[str, ...] = field(default_factory=tuple)


def reciprocal_rank_fusion(
    channel_results: Mapping[str, Sequence[SearchHit]],
    *,
    weights: Mapping[str, float] | None = None,
    rank_constant: int = 60,
    limit: int = 10,
) -> list[SearchHit]:
    """Fuse independent result lists without mixing their storage layers."""

    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")
    if limit < 1:
        return []

    effective_weights = weights or {}
    scores: dict[str, float] = {}
    hits: dict[str, SearchHit] = {}
    seen_channels: dict[str, list[str]] = {}

    for channel, results in channel_results.items():
        weight = float(effective_weights.get(channel, 1.0))
        if weight <= 0:
            continue
        for rank, hit in enumerate(results, start=1):
            hits.setdefault(hit.source_id, hit)
            scores[hit.source_id] = scores.get(hit.source_id, 0.0) + weight / (
                rank_constant + rank
            )
            channels = seen_channels.setdefault(hit.source_id, [])
            if channel not in channels:
                channels.append(channel)

    ordered = sorted(scores, key=lambda item: (-scores[item], item))[:limit]
    return [
        replace(hits[source_id], score=scores[source_id], channels=tuple(seen_channels[source_id]))
        for source_id in ordered
    ]
