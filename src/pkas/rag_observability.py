import json
import math
import uuid
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from typing import Any

from pkas.db import Database
from pkas.repository import utc_now
from pkas.retrieval import RetrievalService
from pkas.vector_index import QdrantVectorIndex


class RagObservabilityService:
    """Read-only vector observability plus a persistent, source-grounded RAG benchmark."""

    def __init__(
        self,
        database: Database,
        vector_index: QdrantVectorIndex,
        retrieval: RetrievalService,
    ) -> None:
        self.database = database
        self.vector_index = vector_index
        self.retrieval = retrieval
        self._status_lock = Lock()
        self._status_snapshot: dict[str, Any] | None = None
        self._status_snapshot_at = 0.0
        self._status_snapshot_updated_at: str | None = None
        self._status_refreshing = False
        self._status_refresh_error: str | None = None
        # The full matrix deliberately reads many real SQLite and Qdrant
        # counters. Keep it off the interactive request path while retaining
        # a short-lived, source-grounded snapshot for the control panel.
        self._status_snapshot_ttl_seconds = 30.0

    def fusion_tuning_status(self, *, minimum_gold_cases: int = 30) -> dict[str, Any]:
        progress = self.review_progress()
        eligible = int(progress.get("eligible", 0))
        ready = eligible >= minimum_gold_cases
        settings = self.vector_index.settings
        return {
            "status": "ready" if ready else "waiting_for_human_gold",
            "eligible_gold_cases": eligible,
            "minimum_gold_cases": minimum_gold_cases,
            "current_weights": {
                "fts": settings.fusion_fts_weight,
                "vector": settings.fusion_vector_weight,
            },
            "candidate_weight_ratios": [
                "0.5:1", "0.75:1", "1:1", "1.25:1", "1.5:1", "2:1"
            ],
            "may_apply": ready,
            "reason": (
                "人工金标达到门槛，可以运行训练/验证分离的权重搜索。"
                if ready
                else "人工金标不足；保持1:1默认权重，禁止用银标直接覆盖正式策略。"
            ),
        }

    def tune_fusion_weights(
        self, *, minimum_gold_cases: int = 30, top_k: int = 5
    ) -> dict[str, Any]:
        """Recommend RRF weights from human gold labels without repeated model calls."""
        cases = sorted(
            (case for case in self.list_cases() if case["review_eligible"]),
            key=lambda item: str(item["id"]),
        )
        if len(cases) < minimum_gold_cases:
            return {
                **self.fusion_tuning_status(minimum_gold_cases=minimum_gold_cases),
                "status": "waiting_for_human_gold",
                "recommendation": None,
            }

        ratios = [(0.5, 1.0), (0.75, 1.0), (1.0, 1.0), (1.25, 1.0), (1.5, 1.0), (2.0, 1.0)]
        prepared: list[dict[str, Any]] = []
        for case in cases:
            response = self.retrieval.search(
                case["query"], domain=case["domain"], limit=100,
                include_restricted=case["include_restricted"],
                include_unverified_claims=False, rerank_mode="never",
                expand_parent=False,
            )
            prepared.append({
                "case": case,
                "candidates": [
                    {
                        "source_id": str(item.get("source_id") or ""),
                        "channel_ranks": dict(item.get("channel_ranks") or {}),
                        "tie": float(item.get("local_tiebreaker") or 0.0),
                    }
                    for item in response.results if item.get("source_id")
                ],
            })

        def is_validation(case_id: str) -> bool:
            return int(sha256(case_id.encode()).hexdigest()[:8], 16) % 5 == 0

        def evaluate(
            rows: list[dict[str, Any]], fts_weight: float, vector_weight: float
        ) -> dict[str, float]:
            totals: dict[str, float] = {"hit": 0.0, "mrr": 0.0, "ndcg": 0.0}
            for row in rows:
                case = row["case"]
                grades = {
                    str(item["source_id"]): int(item["relevance_grade"])
                    for item in case["judgments"]
                    if item.get("judgment_basis") == "human"
                }

                def score(item: dict[str, Any]) -> float:
                    ranks = item["channel_ranks"]
                    lexical = fts_weight / (60 + int(ranks["fts"])) if "fts" in ranks else 0.0
                    semantic = (
                        vector_weight / (60 + int(ranks["vector"]))
                        if "vector" in ranks
                        else 0.0
                    )
                    return lexical + semantic + item["tie"] * 1e-9

                ranked = sorted(row["candidates"], key=score, reverse=True)[:top_k]
                result_grades = [grades.get(item["source_id"], 0) for item in ranked]
                relevant_ranks = [index for index, grade in enumerate(result_grades) if grade > 0]
                totals["hit"] += int(bool(relevant_ranks))
                totals["mrr"] += 1 / (relevant_ranks[0] + 1) if relevant_ranks else 0.0
                totals["ndcg"] += self._ndcg(
                    result_grades, [grade for grade in grades.values() if grade > 0], top_k
                )
            count = max(1, len(rows))
            metrics = {name: totals[name] / count for name in ("hit", "mrr", "ndcg")}
            metrics["objective"] = (
                0.60 * metrics["ndcg"] + 0.25 * metrics["hit"] + 0.15 * metrics["mrr"]
            )
            return metrics

        train = [row for row in prepared if not is_validation(str(row["case"]["id"]))]
        validation = [row for row in prepared if is_validation(str(row["case"]["id"]))]
        if not validation:
            validation = train[-max(1, len(train) // 5):]
            train = train[:-len(validation)] or validation
        scored = [
            {
                "weights": {"fts": fts, "vector": vector},
                "train": evaluate(train, fts, vector),
                "validation": evaluate(validation, fts, vector),
            }
            for fts, vector in ratios
        ]
        recommendation = max(
            scored,
            key=lambda item: (
                item["train"]["objective"], item["validation"]["objective"],
                -abs(item["weights"]["fts"] - 1.0),
            ),
        )
        return {
            "status": "completed",
            "eligible_gold_cases": len(cases),
            "minimum_gold_cases": minimum_gold_cases,
            "top_k": top_k,
            "train_cases": len(train),
            "validation_cases": len(validation),
            "recommendation": recommendation,
            "candidates": scored,
            "applied": False,
            "reason": "已生成离线推荐；修改正式权重前仍需查看留出集指标并明确应用。",
        }

    def _collect_status(self) -> dict[str, Any]:
        coverage = self.vector_index.coverage()
        allow_restricted = bool(
            self.vector_index.settings.embedding_allow_restricted_remote_processing
        )
        with self.database.connect() as connection:
            domains = {
                row["domain"]: row["n"]
                for row in connection.execute(
                    """SELECT c.domain, COUNT(*) AS n FROM vector_index_state v
                    JOIN chunks c ON c.id=v.chunk_id
                    JOIN sources s ON s.id=c.source_id
                    WHERE s.status='indexed'
                      AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                    GROUP BY c.domain ORDER BY c.domain"""
                ).fetchall()
            }
            structure = {
                "blocks": int(connection.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]),
                "chunk_block_relations": int(
                    connection.execute("SELECT COUNT(*) FROM chunk_blocks").fetchone()[0]
                ),
                "orphan_chunks": int(
                    connection.execute(
                        """SELECT COUNT(*) FROM chunks c LEFT JOIN chunk_blocks cb
                        ON cb.chunk_id=c.id WHERE cb.chunk_id IS NULL"""
                    ).fetchone()[0]
                ),
                "chunk_kinds": {
                    row["chunk_kind"]: int(row["n"])
                    for row in connection.execute(
                        """SELECT chunk_kind, COUNT(*) AS n FROM chunks c
                        JOIN sources s ON s.id=c.source_id
                        WHERE s.status='indexed'
                          AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                        GROUP BY chunk_kind ORDER BY chunk_kind"""
                    ).fetchall()
                },
                "chunker_versions": {
                    row["chunker_version"]: int(row["n"])
                    for row in connection.execute(
                        """SELECT chunker_version, COUNT(*) AS n FROM chunks c
                        JOIN sources s ON s.id=c.source_id
                        WHERE s.status='indexed'
                          AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                        GROUP BY chunker_version ORDER BY chunker_version"""
                    ).fetchall()
                },
            }
            matrix_rows = connection.execute(
                """SELECT c.domain, s.source_type, c.privacy,
                COUNT(*) AS total_chunks,
                SUM(CASE WHEN s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                    AND (? OR c.privacy<>'restricted') THEN 1 ELSE 0 END)
                    AS eligible_chunks,
                SUM(CASE WHEN v.chunk_id IS NOT NULL THEN 1 ELSE 0 END)
                    AS indexed_chunks
                FROM chunks c JOIN sources s ON s.id=c.source_id
                LEFT JOIN vector_index_state v ON v.chunk_id=c.id
                    AND v.provider=? AND v.model=? AND v.collection_name=?
                    AND v.payload_version=?
                WHERE s.status='indexed'
                  AND s.source_type NOT IN ('codex-turn','thread-summary','thread-journal')
                GROUP BY c.domain, s.source_type, c.privacy
                ORDER BY c.domain, s.source_type, c.privacy""",
                (
                    int(allow_restricted),
                    self.vector_index.embedding.provider_name,
                    self.vector_index.embedding.model_name,
                    self.vector_index.settings.qdrant_collection,
                    self.vector_index.payload_version,
                ),
            ).fetchall()
            coverage_matrix = []
            for row in matrix_rows:
                item = dict(row)
                eligible = int(item["eligible_chunks"] or 0)
                indexed = int(item["indexed_chunks"] or 0)
                item["total_chunks"] = int(item["total_chunks"] or 0)
                item["eligible_chunks"] = eligible
                item["indexed_chunks"] = indexed
                item["pending_chunks"] = max(0, eligible - indexed)
                item["coverage"] = indexed / eligible if eligible else None
                coverage_matrix.append(item)
            excluded_codex = int(
                connection.execute(
                    """SELECT COUNT(*) FROM chunks c JOIN sources s ON s.id=c.source_id
                    WHERE s.status='indexed' AND s.source_type='codex-turn'"""
                ).fetchone()[0]
            )
            excluded_derived = int(
                connection.execute(
                    """SELECT COUNT(*) FROM chunks c JOIN sources s ON s.id=c.source_id
                    WHERE s.status='indexed'
                      AND s.source_type IN ('thread-summary','thread-journal')"""
                ).fetchone()[0]
            )
            excluded_restricted = sum(
                item["total_chunks"]
                for item in coverage_matrix
                if item["source_type"] != "codex-turn"
                and item["privacy"] == "restricted"
                and not allow_restricted
            )
            indexed_sources = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sources WHERE status='indexed'"
                ).fetchone()[0]
            )
            sources_without_chunks = int(
                connection.execute(
                    """SELECT COUNT(*) FROM sources s WHERE s.status='indexed'
                    AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.source_id=s.id)"""
                ).fetchone()[0]
            )
            retrieval_surfaces = {
                "document_fts": {
                    "records": int(
                        connection.execute(
                            """SELECT COUNT(*) FROM chunks c JOIN sources s
                            ON s.id=c.source_id WHERE s.status='indexed'
                            AND s.source_type NOT IN (
                                'codex-turn','thread-summary','thread-journal'
                            )"""
                        ).fetchone()[0]
                    ),
                    "indexed": int(
                        connection.execute(
                            """SELECT COUNT(*) FROM chunks_fts f JOIN chunks c
                            ON c.id=f.chunk_id JOIN sources s ON s.id=c.source_id
                            WHERE s.status='indexed'
                              AND s.source_type NOT IN (
                                  'codex-turn','thread-summary','thread-journal'
                              )"""
                        ).fetchone()[0]
                    ),
                    "physical_rows": int(
                        connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
                    ),
                    "engine": "SQLite FTS5",
                    "semantic": False,
                },
                "document_vectors": {
                    "records": int(coverage["eligible"]),
                    "indexed": int(coverage["indexed"]),
                    "engine": "Qdrant",
                    "semantic": True,
                },
                "customer_message_fts": {
                    "records": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM customer_messages"
                        ).fetchone()[0]
                    ),
                    "indexed": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM customer_messages_fts"
                        ).fetchone()[0]
                    ),
                    "engine": "SQLite FTS5 / restricted",
                    "semantic": False,
                },
                "source_catalog": {
                    "records": int(
                        connection.execute("SELECT COUNT(*) FROM sync_items").fetchone()[0]
                    ),
                    "indexed": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM sync_items WHERE state<>'missing'"
                        ).fetchone()[0]
                    ),
                    "engine": "SQLite metadata",
                    "semantic": False,
                },
                "approved_knowledge": {
                    "records": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM knowledge_items"
                        ).fetchone()[0]
                    ),
                    "indexed": int(
                        connection.execute(
                            """SELECT COUNT(*) FROM knowledge_items
                            WHERE review_status='approved' AND valid_to IS NULL"""
                        ).fetchone()[0]
                    ),
                    "engine": "SQLite reviewed knowledge",
                    "semantic": False,
                },
            }
        qdrant = self.vector_index.runtime_status()
        coverage_value = coverage["coverage"]
        return {
            "provider": self.vector_index.embedding.provider_name,
            "model": self.vector_index.embedding.model_name,
            "scope": self.vector_index.settings.embedding_scope,
            "collection": self.vector_index.settings.qdrant_collection,
            "eligible_chunks": coverage["eligible"],
            "indexed_chunks": coverage["indexed"],
            "pending_chunks": coverage["pending"],
            "coverage": round(coverage_value, 6) if coverage_value is not None else None,
            "domains": domains,
            "coverage_matrix": coverage_matrix,
            "coverage_scope": {
                "indexed_sources": indexed_sources,
                "all_indexed_chunks": sum(
                    item["total_chunks"] for item in coverage_matrix
                ),
                "vector_eligible_chunks": int(coverage["eligible"]),
                "vector_indexed_chunks": int(coverage["indexed"]),
                "excluded_codex_user_tasks": excluded_codex,
                "excluded_thread_derivatives": excluded_derived,
                "excluded_restricted_policy": excluded_restricted,
                "sources_without_chunks": sources_without_chunks,
                "restricted_embedding_enabled": allow_restricted,
            },
            "retrieval_surfaces": retrieval_surfaces,
            "structure": structure,
            "qdrant": qdrant,
            "mcp_enabled": False,
        }

    def status(self) -> dict[str, Any]:
        """Return the complete, immediately calculated RAG report.

        This is intentionally retained for CLI, tests, and explicit
        diagnostics. Interactive surfaces should call ``status_snapshot`` so
        a multi-GB historical database never leaves the UI waiting.
        """
        return self._collect_status()

    def prewarm_status(self) -> None:
        """Begin a non-blocking refresh as soon as the local service starts."""
        self.status_snapshot()

    def _pending_status(self) -> dict[str, Any]:
        settings = self.vector_index.settings
        return {
            "provider": self.vector_index.embedding.provider_name,
            "model": self.vector_index.embedding.model_name,
            "scope": settings.embedding_scope,
            "collection": settings.qdrant_collection,
            "eligible_chunks": 0,
            "indexed_chunks": 0,
            "pending_chunks": 0,
            "coverage": None,
            "domains": {},
            "coverage_matrix": [],
            "coverage_scope": {
                "indexed_sources": 0,
                "all_indexed_chunks": 0,
                "vector_eligible_chunks": 0,
                "vector_indexed_chunks": 0,
                "excluded_codex_user_tasks": 0,
                "excluded_thread_derivatives": 0,
                "excluded_restricted_policy": 0,
                "sources_without_chunks": 0,
                "restricted_embedding_enabled": bool(
                    settings.embedding_allow_restricted_remote_processing
                ),
            },
            "retrieval_surfaces": {},
            "structure": {
                "blocks": 0,
                "chunk_block_relations": 0,
                "orphan_chunks": 0,
                "chunk_kinds": {},
                "chunker_versions": {},
            },
            "qdrant": {"status": "reading"},
            "mcp_enabled": False,
            "snapshot_state": "refreshing",
            "snapshot_updated_at": None,
            "snapshot_error": None,
        }

    def _refresh_status_snapshot(self) -> None:
        try:
            snapshot = self._collect_status()
        except Exception:
            snapshot = None
        with self._status_lock:
            self._status_refreshing = False
            if snapshot is None:
                self._status_refresh_error = "索引指标刷新失败，将在下次查看时自动重试。"
                return
            self._status_snapshot = snapshot
            self._status_snapshot_at = monotonic()
            self._status_snapshot_updated_at = utc_now()
            self._status_refresh_error = None

    def status_snapshot(self) -> dict[str, Any]:
        """Return cached RAG metrics immediately and refresh them in the background."""
        should_start = False
        with self._status_lock:
            snapshot = deepcopy(self._status_snapshot) if self._status_snapshot else None
            updated_at = self._status_snapshot_updated_at
            age = monotonic() - self._status_snapshot_at if snapshot else None
            fresh = age is not None and age < self._status_snapshot_ttl_seconds
            if not fresh and not self._status_refreshing:
                self._status_refreshing = True
                should_start = True
            refreshing = self._status_refreshing
            refresh_error = self._status_refresh_error
        if should_start:
            Thread(
                target=self._refresh_status_snapshot,
                name="pkas-rag-status-refresh",
                daemon=True,
            ).start()
        if snapshot is None:
            pending = self._pending_status()
            pending["snapshot_error"] = refresh_error
            return pending
        snapshot["snapshot_state"] = "ready" if fresh else "refreshing"
        snapshot["snapshot_updated_at"] = updated_at
        snapshot["snapshot_error"] = refresh_error
        if refreshing and not fresh:
            snapshot["snapshot_state"] = "refreshing"
        return snapshot

    def latest_silver_gate(self) -> dict[str, Any] | None:
        report_path = self.latest_silver_gate_path()
        if report_path is None:
            return None
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.pop("details", None)
        report["report_path"] = str(report_path)
        return report

    def latest_silver_gate_path(self) -> Path | None:
        report_dir = Path(self.vector_index.settings.data_root) / "reports"
        reports = sorted(report_dir.glob("rag-silver-gate-*.json"), reverse=True)
        return reports[0] if reports else None

    @staticmethod
    def _project(vectors: list[list[float]]) -> list[tuple[float, float]]:
        if not vectors:
            return []
        width = len(vectors[0])
        mean = [sum(vector[i] for vector in vectors) / len(vectors) for i in range(width)]
        centered = [[value - mean[i] for i, value in enumerate(vector)] for vector in vectors]

        def norm(vector: list[float]) -> float:
            return math.sqrt(sum(value * value for value in vector))

        axis1 = max(centered, key=norm)
        n1 = norm(axis1) or 1.0
        axis1 = [value / n1 for value in axis1]
        residuals = []
        for vector in centered:
            projection = sum(a * b for a, b in zip(vector, axis1, strict=True))
            residuals.append([v - projection * a for v, a in zip(vector, axis1, strict=True)])
        axis2 = max(residuals, key=norm)
        n2 = norm(axis2) or 1.0
        axis2 = [value / n2 for value in axis2]
        raw = [
            (
                sum(a * b for a, b in zip(vector, axis1, strict=True)),
                sum(a * b for a, b in zip(vector, axis2, strict=True)),
            )
            for vector in centered
        ]
        xs, ys = [item[0] for item in raw], [item[1] for item in raw]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        return [
            (
                0.05 + 0.9 * (x - xmin) / (xmax - xmin or 1.0),
                0.05 + 0.9 * (y - ymin) / (ymax - ymin or 1.0),
            )
            for x, y in raw
        ]

    @staticmethod
    def _stratified_records(records: list[Any], limit: int) -> list[Any]:
        buckets: dict[tuple[str, str], list[Any]] = {}
        for record in records:
            payload = dict(record.payload or {})
            key = (
                str(payload.get("domain") or "shared"),
                str(payload.get("source_type") or "unknown"),
            )
            buckets.setdefault(key, []).append(record)
        for bucket in buckets.values():
            bucket.sort(key=lambda item: str(item.id))
        selected: list[Any] = []
        ordered_keys = sorted(buckets)
        while len(selected) < limit and ordered_keys:
            next_keys = []
            for key in ordered_keys:
                bucket = buckets[key]
                if bucket and len(selected) < limit:
                    selected.append(bucket.pop(0))
                if bucket:
                    next_keys.append(key)
            ordered_keys = next_keys
        return selected

    def vector_map(self, limit: int = 300) -> dict[str, Any]:
        client = self.vector_index._get_client()
        if not self.vector_index._collection_exists(client):
            return {"points": [], "projection": "data-axis-v1", "sampled": 0}
        payload_fields = [
            "chunk_id", "source_id", "domain", "privacy", "source_type"
        ]
        population = []
        offset: Any | None = None
        while True:
            batch, offset = client.scroll(
                collection_name=self.vector_index.settings.qdrant_collection,
                limit=256,
                offset=offset,
                with_payload=payload_fields,
                with_vectors=False,
            )
            population.extend(batch)
            if offset is None:
                break
        selected = self._stratified_records(
            population,
            max(1, min(limit, 500)),
        )
        selected_ids = [point.id for point in selected]
        records = client.retrieve(
            collection_name=self.vector_index.settings.qdrant_collection,
            ids=selected_ids,
            with_payload=payload_fields,
            with_vectors=True,
        )
        hydrated = self.vector_index.repository.hydrate_chunks(
            [str((point.payload or {}).get("chunk_id") or "") for point in records],
            snippet_chars=0,
            ranking_chars=0,
        )
        vectors = [list(point.vector or []) for point in records]
        coordinates = self._project(vectors)
        points = []
        for point, (x, y) in zip(records, coordinates, strict=True):
            payload = dict(point.payload or {})
            chunk = hydrated.get(str(payload.get("chunk_id") or ""), {})
            points.append(
                {
                    "id": payload.get("chunk_id", str(point.id)),
                    "source_id": payload.get("source_id"),
                    "title": str(chunk.get("title") or "未命名片段")[:120],
                    "domain": payload.get("domain", "shared"),
                    "privacy": payload.get("privacy", "private"),
                    "source_type": payload.get("source_type", "unknown"),
                    "chunk_kind": chunk.get("chunk_kind", "unknown"),
                    "char_count": int(chunk.get("char_count") or 0),
                    "radius": round(
                        4.0 + min(5.0, math.log10(max(1, int(chunk.get("char_count") or 0)))),
                        3,
                    ),
                    "x": round(x, 6),
                    "y": round(y, 6),
                }
            )
        return {
            "points": points,
            "sampled": len(points),
            "population": len(population),
            "projection": "balanced-domain-source-v2",
            "sample_distribution": dict(
                Counter(
                    f"{point['domain']}:{point['source_type']}" for point in points
                )
            ),
            "note": (
                "按领域和来源类型平衡抽样；二维坐标和细胞大小只用于观察，"
                "不参与实际检索排序。"
            ),
        }

    def create_case(
        self,
        *,
        query: str,
        expected_source_ids: list[str],
        domain: str | None,
        include_restricted: bool,
        tags: list[str],
        match_policy: str = "any",
        category: str = "general",
        difficulty: str = "normal",
        review_status: str = "reviewed",
        judgments: list[dict[str, Any]] | None = None,
        judgment_scope_source_ids: list[str] | None = None,
        replace_expected: bool = False,
    ) -> dict[str, Any]:
        case_id, now = f"evalcase_{uuid.uuid4().hex}", utc_now()
        scope = sorted(set(judgment_scope_source_ids or []))
        judgment_map: dict[str, dict[str, Any]] = {
            source_id: {
                "source_id": source_id,
                "relevance_grade": 3,
                "judgment_basis": "expected-source",
            }
            for source_id in expected_source_ids
        }
        for judgment in judgments or []:
            judgment_map[str(judgment["source_id"])] = dict(judgment)
        if review_status == "reviewed" and not scope:
            raise ValueError("正式黄金题必须记录本次人工复核的候选来源范围。")

        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id, expected_source_ids_json, tags_json FROM rag_eval_cases "
                "WHERE query=? AND enabled=1 ORDER BY created_at LIMIT 1",
                (query,),
            ).fetchone()
            if existing:
                expected = (
                    sorted(set(expected_source_ids))
                    if replace_expected
                    else sorted(
                        set(json.loads(existing["expected_source_ids_json"]))
                        | set(expected_source_ids)
                    )
                )
                merged_tags = sorted(set(json.loads(existing["tags_json"])) | set(tags))
                saved_case_id = existing["id"]
                status = "updated"
            else:
                saved_case_id = case_id
                expected = sorted(set(expected_source_ids))
                merged_tags = sorted(set(tags))
                status = "created"

            if review_status == "reviewed":
                missing = sorted(set(scope) - set(judgment_map))
                if missing:
                    raise ValueError(f"候选范围仍有 {len(missing)} 个来源未标注。")
                non_human = sorted(
                    source_id
                    for source_id in scope
                    if judgment_map[source_id].get("judgment_basis") != "human"
                )
                if non_human:
                    raise ValueError(
                        f"正式金标仍有 {len(non_human)} 个来源未经人工确认。"
                    )
                if any(
                    int(judgment_map.get(source_id, {}).get("relevance_grade", 0)) <= 0
                    for source_id in expected
                ):
                    raise ValueError("每个期望来源都必须有 1–3 级正相关标注。")

            if existing:
                connection.execute(
                    """UPDATE rag_eval_cases
                    SET expected_source_ids_json=?, domain=?, include_restricted=?,
                        tags_json=?, match_policy=?, category=?, difficulty=?,
                        review_status=?, judgment_scope_source_ids_json=?,
                        reviewed_at=?, updated_at=? WHERE id=?""",
                    (
                        json.dumps(expected), domain, int(include_restricted),
                        json.dumps(merged_tags, ensure_ascii=False), match_policy,
                        category, difficulty, review_status, json.dumps(scope),
                        now if review_status == "reviewed" else None, now, saved_case_id,
                    ),
                )
                if replace_expected:
                    connection.execute(
                        "DELETE FROM rag_eval_judgments WHERE case_id=?",
                        (saved_case_id,),
                    )
            else:
                connection.execute(
                    """INSERT INTO rag_eval_cases(
                    id, query, expected_source_ids_json, domain, include_restricted,
                    tags_json, match_policy, category, difficulty, review_status,
                    judgment_scope_source_ids_json, reviewed_at,
                    enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        case_id, query, json.dumps(expected), domain,
                        int(include_restricted), json.dumps(merged_tags, ensure_ascii=False),
                        match_policy, category, difficulty, review_status, json.dumps(scope),
                        now if review_status == "reviewed" else None, now, now,
                    ),
                )

            for judgment in judgment_map.values():
                connection.execute(
                    """INSERT INTO rag_eval_judgments(
                    id, case_id, source_id, relevance_grade, judgment_basis,
                    reviewer, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'owner', ?, ?)
                    ON CONFLICT(case_id, source_id) DO UPDATE SET
                    relevance_grade=excluded.relevance_grade,
                    judgment_basis=excluded.judgment_basis,
                    reviewer=excluded.reviewer,
                    updated_at=excluded.updated_at""",
                    (
                        f"evaljudgment_{uuid.uuid4().hex}", saved_case_id,
                        judgment["source_id"], int(judgment["relevance_grade"]),
                        judgment.get("judgment_basis", "human"), now, now,
                    ),
                )
            connection.commit()
        result = {
            "id": saved_case_id,
            "query": query,
            "expected_source_ids": expected,
        }
        if status == "updated":
            result["status"] = status
        return result

    def upsert_judgments(
        self, case_id: str, judgments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM rag_eval_cases WHERE id=?", (case_id,)
            ).fetchone()
            if not exists:
                raise ValueError("黄金测评样本不存在。")
            for judgment in judgments:
                connection.execute(
                    """INSERT INTO rag_eval_judgments(
                    id, case_id, source_id, relevance_grade, judgment_basis,
                    reviewer, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'owner', ?, ?)
                    ON CONFLICT(case_id, source_id) DO UPDATE SET
                    relevance_grade=excluded.relevance_grade,
                    judgment_basis=excluded.judgment_basis,
                    reviewer=excluded.reviewer,
                    updated_at=excluded.updated_at""",
                    (
                        f"evaljudgment_{uuid.uuid4().hex}", case_id,
                        judgment["source_id"], int(judgment["relevance_grade"]),
                        judgment.get("judgment_basis", "human"), now, now,
                    ),
                )
            connection.commit()
            count = connection.execute(
                "SELECT COUNT(*) FROM rag_eval_judgments WHERE case_id=?", (case_id,)
            ).fetchone()[0]
        return {"case_id": case_id, "judgment_count": int(count)}

    def list_cases(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM rag_eval_cases WHERE enabled=1 ORDER BY created_at DESC"
            ).fetchall()
            judgments = connection.execute(
                """SELECT case_id, source_id, relevance_grade, judgment_basis, reviewer
                FROM rag_eval_judgments ORDER BY case_id, source_id"""
            ).fetchall()
        by_case: dict[str, list[dict[str, Any]]] = {}
        for judgment in judgments:
            by_case.setdefault(judgment["case_id"], []).append(dict(judgment))
        items = []
        for row in rows:
            item = dict(row)
            item["expected_source_ids"] = json.loads(item.pop("expected_source_ids_json"))
            item["tags"] = json.loads(item.pop("tags_json"))
            item["judgment_scope_source_ids"] = json.loads(
                item.pop("judgment_scope_source_ids_json")
            )
            item["include_restricted"] = bool(item["include_restricted"])
            item["judgments"] = by_case.get(item["id"], [])
            judged_source_ids = {value["source_id"] for value in item["judgments"]}
            human_source_ids = {
                value["source_id"]
                for value in item["judgments"]
                if value["judgment_basis"] == "human"
            }
            positive_source_ids = {
                value["source_id"]
                for value in item["judgments"]
                if int(value["relevance_grade"]) > 0
            }
            scope = set(item["judgment_scope_source_ids"])
            item["review_eligible"] = bool(
                item["review_status"] == "reviewed"
                and scope
                and scope <= judged_source_ids
                and scope <= human_source_ids
                and set(item["expected_source_ids"]) <= positive_source_ids
            )
            item["review_gap"] = {
                "unjudged": len(scope - judged_source_ids),
                "non_human": len(scope - human_source_ids),
                "expected_not_positive": len(
                    set(item["expected_source_ids"]) - positive_source_ids
                ),
            }
            items.append(item)
        return items

    def review_progress(self) -> dict[str, Any]:
        cases = self.list_cases()
        eligible = [case for case in cases if case["review_eligible"]]
        rejected = [case for case in cases if case["review_status"] == "rejected"]
        pending = [
            case
            for case in cases
            if not case["review_eligible"] and case["review_status"] != "rejected"
        ]
        by_category = Counter(str(case.get("category") or "general") for case in pending)
        by_difficulty = Counter(str(case.get("difficulty") or "normal") for case in pending)
        basis_counts = Counter(
            str(judgment.get("judgment_basis") or "unknown")
            for case in cases
            for judgment in case["judgments"]
        )
        for basis in ("human", "expected-source", "source-grounded"):
            basis_counts.setdefault(basis, 0)
        ordered_pending = sorted(
            pending,
            key=lambda case: (str(case.get("created_at") or ""), str(case["id"])),
        )
        total = len(cases)
        return {
            "protocol": "human-graded-scope-v1",
            "total": total,
            "eligible": len(eligible),
            "pending": len(pending),
            "rejected": len(rejected),
            "replacement_needed": len(rejected),
            "draft": sum(case["review_status"] == "draft" for case in pending),
            "reviewed_incomplete": sum(
                case["review_status"] == "reviewed" for case in pending
            ),
            "decision_completion": (
                (len(eligible) + len(rejected)) / total if total else 0.0
            ),
            "gold_completion": min(len(eligible) / 150, 1.0),
            "next_case_id": ordered_pending[0]["id"] if ordered_pending else None,
            "pending_by_category": dict(sorted(by_category.items())),
            "pending_by_difficulty": dict(sorted(by_difficulty.items())),
            "judgment_basis_counts": dict(sorted(basis_counts.items())),
        }

    def reject_case(self, case_id: str, reason_code: str) -> dict[str, Any]:
        allowed = {
            "ambiguous",
            "unanswerable",
            "duplicate",
            "bad_source",
            "out_of_scope",
        }
        if reason_code not in allowed:
            raise ValueError("不支持的金标题剔除原因。")
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT tags_json FROM rag_eval_cases WHERE id=? AND enabled=1",
                (case_id,),
            ).fetchone()
            if not row:
                raise ValueError("黄金测评样本不存在。")
            tags = sorted(
                set(json.loads(row["tags_json"]))
                | {"human-rejected-v1", f"rejection:{reason_code}"}
            )
            connection.execute(
                """UPDATE rag_eval_cases
                SET review_status='rejected', tags_json=?, reviewed_at=?, updated_at=?
                WHERE id=?""",
                (json.dumps(tags, ensure_ascii=False), now, now, case_id),
            )
            connection.commit()
        saved = next(item for item in self.list_cases() if item["id"] == case_id)
        saved["rejection_reason_code"] = reason_code
        return saved

    @staticmethod
    def _safe_review_candidate(item: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "source_id",
            "document_id",
            "chunk_id",
            "title",
            "snippet",
            "locator",
            "domain",
            "privacy",
            "source_type",
            "match_strategy",
            "retrieval_channels",
            "fts_score",
            "vector_score",
            "rerank_score",
        )
        return {key: item.get(key) for key in keys if item.get(key) is not None}

    def _source_preview(self, source_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT s.id AS source_id, s.source_type, s.domain, s.privacy,
                COALESCE(c.title, d.title, s.original_name) AS title,
                c.id AS chunk_id, c.document_id, c.locator, c.text_content
                FROM sources s
                LEFT JOIN documents d ON d.source_id=s.id
                LEFT JOIN chunks c ON c.document_id=d.id
                WHERE s.id=? AND s.status='indexed'
                ORDER BY c.sequence, d.created_at LIMIT 1""",
                (source_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        text = str(item.pop("text_content") or "")
        item["snippet"] = text[:1200]
        return item

    def review_context(
        self,
        case_id: str,
        *,
        limit: int = 10,
        rerank_mode: str = "auto",
    ) -> dict[str, Any]:
        case = next((item for item in self.list_cases() if item["id"] == case_id), None)
        if case is None:
            raise ValueError("黄金测评样本不存在。")
        response = self.retrieval.search(
            case["query"],
            domain=case["domain"],
            limit=limit,
            include_restricted=case["include_restricted"],
            include_unverified_claims=False,
            rerank_mode=rerank_mode,  # type: ignore[arg-type]
            expand_parent=False,
        )
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rank, raw in enumerate(response.results, start=1):
            source_id = str(raw.get("source_id") or "")
            if not source_id or source_id in seen:
                continue
            item = self._safe_review_candidate(raw)
            item.update({"rank": rank, "retrieved": True, "seeded": False})
            candidates.append(item)
            seen.add(source_id)

        required_ids = list(
            dict.fromkeys(
                list(case["expected_source_ids"])
                + list(case["judgment_scope_source_ids"])
            )
        )
        for source_id in required_ids:
            if source_id in seen:
                continue
            preview = self._source_preview(source_id)
            if preview is None:
                preview = {
                    "source_id": source_id,
                    "title": "来源当前不可读取",
                    "snippet": "",
                }
            item = self._safe_review_candidate(preview)
            item.update({"rank": None, "retrieved": False, "seeded": True})
            candidates.append(item)
            seen.add(source_id)

        judgment_map = {
            str(item["source_id"]): item for item in case["judgments"]
        }
        human_judged = {
            source_id
            for source_id, judgment in judgment_map.items()
            if judgment["judgment_basis"] == "human"
        }
        scope = [str(item["source_id"]) for item in candidates]
        return {
            "case": case,
            "candidates": candidates,
            "judgments": judgment_map,
            "scope_source_ids": scope,
            "human_judged_count": len(set(scope) & human_judged),
            "scope_count": len(scope),
            "retrieval": {
                "mode": response.mode,
                "warnings": response.warnings,
                "health": (
                    {
                        "sufficiency": response.health.sufficiency,
                        "reasons": response.health.reasons,
                        "channels": response.health.channels,
                        "candidate_count": response.health.candidate_count,
                        "result_count": response.health.result_count,
                        "source_count": response.health.source_count,
                        "vector_coverage": response.health.vector_coverage,
                        "rerank_status": response.health.rerank_status,
                        "rerank_model": response.health.rerank_model,
                    }
                    if response.health
                    else None
                ),
            },
        }

    def finalize_review(
        self,
        case_id: str,
        *,
        judgments: list[dict[str, Any]],
        judgment_scope_source_ids: list[str],
        category: str,
        difficulty: str,
        match_policy: str,
    ) -> dict[str, Any]:
        scope = list(dict.fromkeys(str(value) for value in judgment_scope_source_ids))
        judgment_map = {str(item["source_id"]): dict(item) for item in judgments}
        if not scope or set(scope) != set(judgment_map):
            raise ValueError("人工标注必须完整覆盖本次候选来源范围。")
        if any(item.get("judgment_basis") != "human" for item in judgment_map.values()):
            raise ValueError("正式金标的每个候选判断都必须由人工确认。")
        positive = sorted(
            source_id
            for source_id, item in judgment_map.items()
            if int(item["relevance_grade"]) > 0
        )
        if not positive:
            raise ValueError("正式金标题至少需要一个人工确认的相关来源。")
        now = utc_now()
        with self.database.connect() as connection:
            case = connection.execute(
                "SELECT tags_json FROM rag_eval_cases WHERE id=? AND enabled=1",
                (case_id,),
            ).fetchone()
            if not case:
                raise ValueError("黄金测评样本不存在。")
            existing_sources = {
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM sources WHERE id IN ({','.join('?' for _ in scope)})",
                    scope,
                ).fetchall()
            }
            missing_sources = sorted(set(scope) - existing_sources)
            if missing_sources:
                raise ValueError(f"有 {len(missing_sources)} 个候选来源已不存在。")
            tags = sorted(set(json.loads(case["tags_json"])) | {"human-reviewed-v1"})
            connection.execute(
                """UPDATE rag_eval_cases SET expected_source_ids_json=?, tags_json=?,
                match_policy=?, category=?, difficulty=?, review_status='reviewed',
                judgment_scope_source_ids_json=?, reviewed_at=?, updated_at=?
                WHERE id=?""",
                (
                    json.dumps(positive),
                    json.dumps(tags, ensure_ascii=False),
                    match_policy,
                    category,
                    difficulty,
                    json.dumps(scope),
                    now,
                    now,
                    case_id,
                ),
            )
            connection.execute("DELETE FROM rag_eval_judgments WHERE case_id=?", (case_id,))
            connection.executemany(
                """INSERT INTO rag_eval_judgments(
                id, case_id, source_id, relevance_grade, judgment_basis,
                reviewer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'human', 'owner', ?, ?)""",
                [
                    (
                        f"evaljudgment_{uuid.uuid4().hex}",
                        case_id,
                        source_id,
                        int(judgment_map[source_id]["relevance_grade"]),
                        now,
                        now,
                    )
                    for source_id in scope
                ],
            )
            connection.commit()
        saved = next(item for item in self.list_cases() if item["id"] == case_id)
        if not saved["review_eligible"]:
            raise RuntimeError("人工复核已保存，但正式门禁未通过。")
        return saved

    @staticmethod
    def _ndcg(grades: list[int], ideal_grades: list[int], top_k: int) -> float:
        def dcg(values: list[int]) -> float:
            return sum(
                ((2**grade) - 1) / math.log2(index + 2)
                for index, grade in enumerate(values[:top_k])
            )

        ideal = dcg(sorted(ideal_grades, reverse=True))
        return dcg(grades) / ideal if ideal else 0.0

    def run_eval(
        self,
        top_k: int,
        rerank_mode: str = "auto",
        retrieval_mode: str = "ab",
    ) -> dict[str, Any]:
        cases = [case for case in self.list_cases() if case["review_eligible"]]
        if not cases:
            return {"status": "warning", "case_count": 0, "warning": "尚未建立黄金测评样本。"}
        run_id, now = f"evalrun_{uuid.uuid4().hex}", utc_now()
        totals: dict[str, float] = {
            "hit": 0.0,
            "recall": 0.0,
            "precision": 0.0,
            "mrr": 0.0,
            "ndcg": 0.0,
            "judgment_coverage": 0.0,
        }
        mode_counts: Counter[str] = Counter()
        strata_totals: dict[str, dict[str, float]] = {}
        details = []
        for case in cases:
            response = self.retrieval.search(
                case["query"], domain=case["domain"], limit=top_k,
                include_restricted=case["include_restricted"],
                include_unverified_claims=False,
                rerank_mode=rerank_mode,  # type: ignore[arg-type]
                expand_parent=False,
                retrieval_mode=retrieval_mode,  # type: ignore[arg-type]
            )
            returned = [str(item.get("source_id") or "") for item in response.results]
            expected = set(case["expected_source_ids"])
            judgment_map = {
                item["source_id"]: int(item["relevance_grade"])
                for item in case["judgments"]
            }
            hits = [index for index, source_id in enumerate(returned) if source_id in expected]
            matched = len(set(returned) & expected)
            recall = (
                float(matched > 0)
                if case.get("match_policy", "any") == "any"
                else matched / len(expected) if expected else 0.0
            )
            returned_grades = [judgment_map.get(source_id) for source_id in returned]
            judged_grades = [grade for grade in returned_grades if grade is not None]
            precision = (
                sum(int(grade > 0) for grade in judged_grades) / len(judged_grades)
                if judged_grades else 0.0
            )
            judgment_coverage = len(judged_grades) / len(returned) if returned else 0.0
            ndcg = self._ndcg(
                [grade or 0 for grade in returned_grades],
                [
                    int(item["relevance_grade"])
                    for item in case["judgments"]
                    if int(item["relevance_grade"]) > 0
                ],
                top_k,
            )
            reciprocal_rank = 1 / (hits[0] + 1) if hits else 0.0
            hit = int(bool(hits))
            totals["hit"] += hit
            totals["recall"] += recall
            totals["precision"] += precision
            totals["mrr"] += reciprocal_rank
            totals["ndcg"] += ndcg
            totals["judgment_coverage"] += judgment_coverage
            stratum = f"{case['category']}:{case['difficulty']}"
            stratum_totals = strata_totals.setdefault(
                stratum,
                {
                    "hit": 0.0,
                    "recall": 0.0,
                    "precision": 0.0,
                    "mrr": 0.0,
                    "ndcg": 0.0,
                    "judgment_coverage": 0.0,
                    "count": 0.0,
                },
            )
            stratum_totals["hit"] += hit
            stratum_totals["recall"] += recall
            stratum_totals["precision"] += precision
            stratum_totals["mrr"] += reciprocal_rank
            stratum_totals["ndcg"] += ndcg
            stratum_totals["judgment_coverage"] += judgment_coverage
            stratum_totals["count"] += 1
            mode_counts[response.mode] += 1
            details.append(
                (
                    case, returned, response.warnings, hit, recall, precision,
                    reciprocal_rank, ndcg, judgment_coverage,
                )
            )
        count = len(cases)
        metrics = {
            "hit_rate": totals["hit"] / count,
            "recall_at_k": totals["recall"] / count,
            "precision_at_k": totals["precision"] / count,
            "mrr": totals["mrr"] / count,
            "ndcg_at_k": totals["ndcg"] / count,
            "judgment_coverage": totals["judgment_coverage"] / count,
        }
        strata = {
            name: {
                "case_count": int(values["count"]),
                "hit_rate": values["hit"] / values["count"],
                "recall_at_k": values["recall"] / values["count"],
                "precision_at_k": values["precision"] / values["count"],
                "mrr": values["mrr"] / values["count"],
                "ndcg_at_k": values["ndcg"] / values["count"],
                "judgment_coverage": values["judgment_coverage"] / values["count"],
            }
            for name, values in strata_totals.items()
        }
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO rag_eval_runs(
                id, status, top_k, case_count, hit_rate, recall_at_k, precision_at_k,
                mrr, ndcg_at_k, judgment_coverage, strata_json,
                eval_protocol, retrieval_modes_json, created_at
                ) VALUES (?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, top_k, count, metrics["hit_rate"], metrics["recall_at_k"],
                 metrics["precision_at_k"], metrics["mrr"], metrics["ndcg_at_k"],
                 metrics["judgment_coverage"], json.dumps(strata),
                 f"graded-scope-v3-{retrieval_mode}", json.dumps(mode_counts), now),
            )
            connection.executemany(
                """INSERT INTO rag_eval_results(
                id, run_id, case_id, hit, recall_at_k, precision_at_k,
                reciprocal_rank, ndcg_at_k, judgment_coverage,
                returned_source_ids_json, warning_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (f"evalresult_{uuid.uuid4().hex}", run_id, case["id"], hit,
                     recall, precision, rr, ndcg, coverage, json.dumps(returned),
                     json.dumps(warnings, ensure_ascii=False), now)
                    for case, returned, warnings, hit, recall, precision, rr, ndcg, coverage
                    in details
                ],
            )
            connection.commit()
        return {
            "status": "completed",
            "id": run_id,
            "top_k": top_k,
            "case_count": count,
            "rerank_mode": rerank_mode,
            "retrieval_mode": retrieval_mode,
            "eval_protocol": f"graded-scope-v3-{retrieval_mode}",
            **metrics,
            "retrieval_modes": dict(mode_counts),
            "strata": strata,
            "metric_status": (
                "complete" if metrics["judgment_coverage"] >= 1.0 else "partial"
            ),
            "precision_note": (
                "P@K 仅在逐结果标注覆盖率达到 100% 时可视为完整精确率；"
                "当前同时返回 judgment_coverage。"
            ),
        }

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM rag_eval_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["retrieval_modes"] = json.loads(item.pop("retrieval_modes_json"))
            item["strata"] = json.loads(item.pop("strata_json"))
            item["metric_status"] = (
                "complete" if item["judgment_coverage"] >= 1.0 else "partial"
            )
            items.append(item)
        return items
