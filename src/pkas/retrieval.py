import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

from pkas.repository import Repository
from pkas.reranker import Reranker, RerankError
from pkas.vector_index import QdrantVectorIndex, VectorIndexError

RerankMode = Literal["auto", "never", "always"]
RetrievalMode = Literal["a", "b", "ab"]
RerankStatus = Literal["not_needed", "disabled", "applied", "degraded"]


@dataclass(slots=True)
class RetrievalHealth:
    sufficiency: Literal["sufficient", "partial", "insufficient"]
    reasons: list[str]
    channels: dict[str, int]
    candidate_count: int
    result_count: int
    source_count: int
    vector_coverage: float
    rerank_status: RerankStatus
    rerank_model: str | None = None
    rerank_candidate_count: int = 0
    rerank_input_chars: int = 0
    fusion_weights: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalResponse:
    results: list[dict[str, Any]]
    mode: str
    warnings: list[str] = field(default_factory=list)
    health: RetrievalHealth | None = None


class RetrievalService:
    def __init__(
        self,
        repository: Repository,
        vector_index: QdrantVectorIndex,
        reranker: Reranker | None = None,
        machine_catalog: Any | None = None,
    ) -> None:
        self.repository = repository
        self.vector_index = vector_index
        self.reranker = reranker
        self.machine_catalog = machine_catalog

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        latin = re.findall(r"[A-Za-z0-9_.:/\\-]{2,}", query.lower())
        chinese = re.findall(r"[\u4e00-\u9fff]{2,}", query)
        return list(dict.fromkeys(latin + chinese))[:24]

    @staticmethod
    def _bounded_source_hint(item: dict[str, Any], *, max_parts: int = 5) -> str:
        """Return a useful code/document locator without exposing an absolute root."""
        raw_uri = str(item.get("original_uri") or "").strip().replace("\\", "/")
        if not raw_uri:
            return ""
        parts = [part for part in PurePosixPath(raw_uri).parts if part not in {"/", "."}]
        if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
            parts = parts[1:]
        return "/".join(parts[-max_parts:])

    @classmethod
    def _local_tiebreaker(cls, query: str, item: dict[str, Any]) -> float:
        text = re.sub(
            r"\s+",
            "",
            (
                f"{item.get('title', '')}\n{cls._bounded_source_hint(item)}\n"
                f"{item.get('snippet', '')}"
            ).lower(),
        )
        score = 0.0
        for term in cls._query_terms(query):
            normalized = re.sub(r"\s+", "", term.lower())
            if normalized and normalized in text:
                score += 1.0
                if normalized in str(item.get("title", "")).lower():
                    score += 0.5
        return score

    @staticmethod
    def _should_rerank(query: str, candidate_count: int, mode: RerankMode) -> bool:
        if mode == "never":
            return False
        if mode == "always":
            return candidate_count >= 2
        markers = (
            "比较",
            "对比",
            "结合",
            "综合",
            "分别",
            "差异",
            "为什么",
            "如何选择",
            "哪些资料",
            "哪些",
            "多个",
        )
        return candidate_count >= 8 and (
            len(query) >= 18 or sum(marker in query for marker in markers) >= 1
        )

    def _rerank_limit(self, query: str, candidate_count: int) -> int:
        deep_markers = ("比较", "对比", "综合", "结合", "分别", "差异", "跨文档", "所有")
        is_deep = len(query) >= 32 or sum(marker in query for marker in deep_markers) >= 2
        configured = (
            self.vector_index.settings.rerank_complex_candidate_limit
            if is_deep
            else self.vector_index.settings.rerank_candidate_limit
        )
        return min(candidate_count, max(2, configured))

    @staticmethod
    def _collapse_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for item in items:
            source_id = str(item.get("source_id") or item.get("document_id") or "unknown")
            representative = grouped.get(source_id)
            if representative is None:
                representative = dict(item)
                representative["supporting_chunk_ids"] = []
                grouped[source_id] = representative
            chunk_id = str(item.get("chunk_id") or "")
            supporting = representative["supporting_chunk_ids"]
            if chunk_id and chunk_id not in supporting and len(supporting) < 3:
                supporting.append(chunk_id)
            channels = set(representative.get("retrieval_channels", []))
            channels.update(item.get("retrieval_channels", []))
            representative["retrieval_channels"] = sorted(channels)
            ranks = dict(representative.get("channel_ranks", {}))
            for channel, rank in item.get("channel_ranks", {}).items():
                ranks[channel] = min(int(rank), int(ranks.get(channel, rank)))
            representative["channel_ranks"] = ranks
            representative["source_evidence_count"] = (
                int(representative.get("source_evidence_count", 0)) + 1
            )
        return list(grouped.values())

    def _apply_rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], RerankStatus, str | None, int, int]:
        if any(item.get("privacy") == "restricted" for item in candidates):
            return (
                candidates,
                "disabled",
                "restricted 候选未发送到云端 Rerank，已保留本地 RRF 顺序。",
                0,
                0,
            )
        if not self.reranker or not self.reranker.enabled:
            return candidates, "disabled", None, 0, 0
        limit = self._rerank_limit(query, len(candidates))
        rerank_candidates = candidates[:limit]
        all_chunk_ids = [
            str(chunk_id)
            for item in rerank_candidates
            for chunk_id in item.get("supporting_chunk_ids", [])
        ]
        hydrated = self.repository.hydrate_chunks(all_chunk_ids)
        documents = []
        remaining_chars = self.vector_index.settings.rerank_total_char_budget
        for item in rerank_candidates:
            if remaining_chars <= 0:
                break
            hydrated_items = [
                hydrated.get(str(chunk_id), {}) for chunk_id in item.get("supporting_chunk_ids", [])
            ]
            source_hint = self._bounded_source_hint(
                next((value for value in hydrated_items if value), item)
            )
            header_parts = [
                f"Source path: {source_hint}" if source_hint else "",
                f"Title: {item.get('title')}" if item.get("title") else "",
            ]
            pieces = [
                "\n".join(
                    part
                    for part in (
                        f"Section: {value.get('title')}" if value.get("title") else "",
                        f"Locator: {value.get('locator')}" if value.get("locator") else "",
                        str(value.get("ranking_text") or ""),
                    )
                    if part
                )
                for value in hydrated_items
            ]
            combined = "\n".join(part for part in header_parts if part)
            if pieces:
                combined = f"{combined}\n\n" if combined else ""
                combined += "\n\n".join(piece for piece in pieces if piece)
            combined = (combined or str(item.get("snippet") or ""))[
                : min(self.vector_index.settings.rerank_document_char_limit, remaining_chars)
            ]
            documents.append(combined)
            remaining_chars -= len(combined)
        rerank_candidates = rerank_candidates[: len(documents)]
        input_chars = sum(len(document) for document in documents)
        if any(not document for document in documents):
            return (
                candidates,
                "degraded",
                "部分候选缺少正文，已保留 RRF 顺序。",
                len(documents),
                input_chars,
            )
        try:
            response = self.reranker.rerank(query, documents, top_n=limit)
        except RerankError as exc:
            return (
                candidates,
                "degraded",
                str(exc) + " 已保留 RRF 顺序。",
                len(documents),
                input_chars,
            )
        reranked: list[dict[str, Any]] = []
        used: set[int] = set()
        # Treat the cross-encoder as a second ranking signal, not as an oracle.
        # Equal reciprocal-rank fusion preserves strong local evidence while still
        # allowing semantically better candidates to move up.
        rerank_weight = 0.50
        local_weight = 1.0 - rerank_weight
        for rerank_rank, (index, score) in enumerate(response.scores, start=1):
            item = dict(rerank_candidates[index])
            item["rerank_score"] = score
            item["rerank_rank"] = rerank_rank
            item["rerank_fusion_score"] = rerank_weight / (10 + rerank_rank) + local_weight / (
                10 + index + 1
            )
            item["retrieval_channels"] = sorted(
                set(item.get("retrieval_channels", [])) | {"rerank"}
            )
            reranked.append(item)
            used.add(index)
        reranked.sort(
            key=lambda item: float(item.get("rerank_fusion_score", 0.0)),
            reverse=True,
        )
        reranked.extend(item for index, item in enumerate(rerank_candidates) if index not in used)
        reranked.extend(candidates[limit:])
        return reranked, "applied", None, len(documents), input_chars

    def _health(
        self,
        results: list[dict[str, Any]],
        *,
        candidate_count: int,
        rerank_status: RerankStatus,
        rerank_warning: str | None,
        coverage: float,
        rerank_candidate_count: int,
        rerank_input_chars: int,
    ) -> RetrievalHealth:
        channels: Counter[str] = Counter()
        for item in results:
            channels.update(item.get("retrieval_channels", []))
        source_count = len(
            {
                str(item.get("source_id") or item.get("document_id") or "")
                for item in results
                if item.get("source_id") or item.get("document_id")
            }
        )
        reasons: list[str] = []
        sufficiency: Literal["sufficient", "partial", "insufficient"] = "sufficient"
        if not results:
            sufficiency = "insufficient"
            reasons.append("没有找到允许访问且可定位的证据。")
        else:
            top = results[0]
            top_channels = set(top.get("retrieval_channels", []))
            if top.get("rerank_score") is not None and float(top["rerank_score"]) < 0.15:
                sufficiency = "insufficient"
                reasons.append("重排模型认为最高候选的相关度仍然很低。")
            elif (
                top_channels == {"vector"}
                and top.get("vector_score") is not None
                and float(top["vector_score"]) < 0.3
            ):
                sufficiency = "partial"
                reasons.append("当前只有低相似度语义证据，没有精确全文命中。")
        if coverage < 0.95:
            sufficiency = "partial" if sufficiency == "sufficient" else sufficiency
            reasons.append(f"语义索引覆盖率只有 {coverage:.1%}，结果可能遗漏资料。")
        if rerank_warning:
            reasons.append(rerank_warning)
        return RetrievalHealth(
            sufficiency=sufficiency,
            reasons=list(dict.fromkeys(reasons)),
            channels=dict(channels),
            candidate_count=candidate_count,
            result_count=len(results),
            source_count=source_count,
            vector_coverage=coverage,
            rerank_status=rerank_status,
            rerank_model=self.reranker.model_name if self.reranker else None,
            rerank_candidate_count=rerank_candidate_count,
            rerank_input_chars=rerank_input_chars,
            fusion_weights={
                "fts": self.vector_index.settings.fusion_fts_weight,
                "catalog": self.vector_index.settings.fusion_fts_weight,
                "vector": self.vector_index.settings.fusion_vector_weight,
            },
        )

    def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        limit: int = 10,
        include_restricted: bool = False,
        include_unverified_claims: bool = False,
        rerank_mode: RerankMode = "auto",
        expand_parent: bool = True,
        include_task_records: bool = False,
        retrieval_mode: RetrievalMode = "ab",
    ) -> RetrievalResponse:
        candidate_limit = min(100, max(limit * 12, 80))
        lexical = []
        if retrieval_mode in {"a", "ab"}:
            lexical = self.repository.search(
                query,
                domain=domain,
                limit=candidate_limit,
                include_restricted=include_restricted,
                include_unverified_claims=include_unverified_claims,
                include_task_records=include_task_records,
            )
            if domain is None and self.machine_catalog is not None:
                lexical.extend(self.machine_catalog.search(query, candidate_limit))
        warnings: list[str] = []
        semantic_items: list[dict[str, Any]] = []
        if retrieval_mode in {"b", "ab"}:
            try:
                semantic = self.vector_index.search(
                    query,
                    domain=domain,
                    limit=candidate_limit,
                    include_restricted=include_restricted,
                )
            except VectorIndexError as exc:
                warnings.append(str(exc))
            else:
                semantic_items = semantic.items
                if semantic.warning:
                    warnings.append(semantic.warning)

        if not include_task_records:
            semantic_items = [
                item for item in semantic_items if item.get("source_type") != "codex-turn"
            ]
        merged: dict[str, dict[str, Any]] = {}
        channel_weights = {
            "fts": max(0.0, self.vector_index.settings.fusion_fts_weight),
            "catalog": max(0.0, self.vector_index.settings.fusion_fts_weight),
            "vector": max(0.0, self.vector_index.settings.fusion_vector_weight),
        }
        lexical_channels = [
            ("catalog" if item.get("source_type") == "filesystem-catalog" else "fts", item)
            for item in lexical
        ]
        streams: list[tuple[str, list[dict[str, Any]]]] = []
        for channel in ("fts", "catalog"):
            streams.append(
                (
                    channel,
                    [item for item_channel, item in lexical_channels if item_channel == channel],
                )
            )
        streams.append(("vector", semantic_items))
        for channel, items in streams:
            for rank, item in enumerate(items, start=1):
                key = str(item.get("chunk_id") or item.get("source_id") or item.get("document_id"))
                if not key:
                    continue
                target = merged.setdefault(key, dict(item))
                target["hybrid_score"] = float(target.get("hybrid_score", 0.0)) + (
                    channel_weights[channel] / (60 + rank)
                )
                channel_ranks = dict(target.get("channel_ranks", {}))
                channel_ranks[channel] = min(rank, int(channel_ranks.get(channel, rank)))
                target["channel_ranks"] = channel_ranks
                channels = set(target.get("retrieval_channels", []))
                channels.add(channel)
                target["retrieval_channels"] = sorted(channels)
                if channel == "fts":
                    vector_score = target.get("vector_score")
                    target.update(item)
                    if vector_score is not None:
                        target["vector_score"] = vector_score
                elif "vector_score" in item:
                    target["vector_score"] = item["vector_score"]
        candidates = list(merged.values())
        for item in candidates:
            item["local_tiebreaker"] = self._local_tiebreaker(query, item)
        candidates.sort(
            key=lambda item: (
                float(item.get("hybrid_score", 0.0)),
                float(item.get("local_tiebreaker", 0.0)),
                float(item.get("vector_score", 0.0)),
            ),
            reverse=True,
        )
        chunk_candidate_count = len(candidates)
        candidates = self._collapse_sources(candidates)
        rerank_status: RerankStatus = "not_needed"
        rerank_warning = None
        rerank_candidate_count = 0
        rerank_input_chars = 0
        if retrieval_mode == "ab" and self._should_rerank(query, len(candidates), rerank_mode):
            (
                candidates,
                rerank_status,
                rerank_warning,
                rerank_candidate_count,
                rerank_input_chars,
            ) = self._apply_rerank(query, candidates)
        results = candidates[:limit]
        if expand_parent:
            for item in results:
                chunk_id = str(item.get("chunk_id") or "")
                if chunk_id:
                    item["parent_context"] = self.repository.expand_chunk_context(
                        chunk_id,
                        radius=1,
                        max_chars=4000,
                    )
        coverage = (
            self.vector_index.coverage()["coverage"]
            if retrieval_mode in {"b", "ab"} and self.vector_index.enabled
            else 1.0
        )
        health = self._health(
            results,
            candidate_count=chunk_candidate_count,
            rerank_status=rerank_status,
            rerank_warning=rerank_warning,
            coverage=coverage,
            rerank_candidate_count=rerank_candidate_count,
            rerank_input_chars=rerank_input_chars,
        )
        warnings.extend(health.reasons)
        mode = {
            "a": "a_lexical_catalog",
            "b": "b_vector",
            "ab": "hybrid" if semantic_items else "fts_only",
        }[retrieval_mode]
        if rerank_status == "applied":
            mode += "_rerank"
        return RetrievalResponse(
            results=results,
            mode=mode,
            warnings=list(dict.fromkeys(warnings)),
            health=health,
        )

    def sync_vector_index(self, *, max_chunks: int | None = None) -> dict[str, Any]:
        try:
            return self.vector_index.sync(max_chunks=max_chunks)
        except VectorIndexError as exc:
            return {"status": "warning", "indexed": 0, "warning": str(exc)}
