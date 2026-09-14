import hashlib
import uuid
import warnings
from dataclasses import dataclass
from typing import Any

from pkas.config import Settings
from pkas.db import Database
from pkas.embeddings import EmbeddingError, EmbeddingProvider
from pkas.repository import Repository, utc_now


class VectorIndexError(RuntimeError):
    """A safe vector-index failure suitable for user-facing health messages."""


def _point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pkas:chunk:{chunk_id}"))


def _source_path_hint(original_uri: str) -> str:
    """Return a bounded path label that disambiguates same-named source files."""
    normalized = str(original_uri or "").replace("\\", "/").strip("/")
    if not normalized or "://" in normalized:
        return ""
    parts = [part for part in normalized.split("/") if part]
    return "/".join(parts[-5:])


def _embedding_text(item: dict[str, Any]) -> str:
    path_hint = _source_path_hint(str(item.get("original_uri") or ""))
    labels = [str(item["title"])]
    if path_hint:
        labels.append(f"Source path: {path_hint}")
    labels.append(str(item["text_content"]))
    return "\n".join(labels)


def _content_hash(item: dict[str, Any]) -> str:
    return hashlib.sha256(_embedding_text(item).encode()).hexdigest()


@dataclass(slots=True)
class VectorSearchResult:
    items: list[dict[str, Any]]
    status: str
    warning: str | None = None


class QdrantVectorIndex:
    provider_name = "qdrant"
    payload_version = 2

    def __init__(
        self,
        settings: Settings,
        database: Database,
        repository: Repository,
        embedding: EmbeddingProvider,
    ) -> None:
        self.settings = settings
        self.database = database
        self.repository = repository
        self.embedding = embedding
        self._client: Any | None = None

    @property
    def enabled(self) -> bool:
        return self.embedding.enabled

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            raise VectorIndexError("Qdrant 客户端尚未安装。") from None
        api_key = (
            self.settings.qdrant_api_key.get_secret_value()
            if self.settings.qdrant_api_key
            else None
        )
        self._client = QdrantClient(
            url=self.settings.qdrant_url,
            api_key=api_key,
            timeout=self.settings.embedding_timeout_seconds,
        )
        return self._client

    def _collection_exists(self, client: Any) -> bool:
        try:
            return bool(client.collection_exists(self.settings.qdrant_collection))
        except Exception:
            raise VectorIndexError("本机 Qdrant 服务不可用。") from None

    def _ensure_collection(self, client: Any, dimension: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        if self._collection_exists(client):
            info = client.get_collection(self.settings.qdrant_collection)
            configured = info.config.params.vectors
            size = getattr(configured, "size", None)
            if size is not None and int(size) != dimension:
                raise VectorIndexError(
                    "现有 Qdrant 集合的向量维度与当前模型不一致，需要显式重建索引。"
                )
            self._ensure_payload_indexes(client)
            return
        client.create_collection(
            collection_name=self.settings.qdrant_collection,
            vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
        )
        self._ensure_payload_indexes(client)

    def _ensure_payload_indexes(self, client: Any) -> None:
        from qdrant_client.models import PayloadSchemaType

        for field in (
            "domain",
            "privacy",
            "source_type",
            "document_id",
            "status",
            "embedding_version",
        ):
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message="Payload indexes have no effect in the local Qdrant.*",
                    )
                    client.create_payload_index(
                        collection_name=self.settings.qdrant_collection,
                        field_name=field,
                        field_schema=PayloadSchemaType.KEYWORD,
                        wait=True,
                    )
            except Exception:
                # In-memory test clients and older Qdrant builds may not implement indexes.
                continue

    def _candidates(self) -> list[dict[str, Any]]:
        clauses = ["s.status = 'indexed'", "s.source_type <> 'codex-turn'"]
        if not self.settings.embedding_allow_restricted_remote_processing:
            clauses.append("c.privacy <> 'restricted'")
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, c.document_id, c.source_id, c.title,
                       c.text_content, c.locator, c.domain, c.privacy,
                       s.original_uri, s.vault_path, s.source_type
                FROM chunks c JOIN sources s ON s.id = c.source_id
                WHERE {' AND '.join(clauses)}
                ORDER BY c.created_at DESC, c.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def coverage(self) -> dict[str, Any]:
        clauses = ["s.status = 'indexed'", "s.source_type <> 'codex-turn'"]
        if not self.settings.embedding_allow_restricted_remote_processing:
            clauses.append("c.privacy <> 'restricted'")
        with self.database.connect() as connection:
            eligible = int(
                connection.execute(
                    f"""SELECT COUNT(*) AS n FROM chunks c
                    JOIN sources s ON s.id=c.source_id
                    WHERE {' AND '.join(clauses)}"""
                ).fetchone()["n"]
            )
            indexed = int(
                connection.execute(
                    """SELECT COUNT(*) AS n FROM vector_index_state
                    WHERE provider=? AND model=? AND collection_name=?
                      AND payload_version=?""",
                    (
                        self.embedding.provider_name,
                        self.embedding.model_name,
                        self.settings.qdrant_collection,
                        self.payload_version,
                    ),
                ).fetchone()["n"]
            )
        return {
            "eligible": eligible,
            "indexed": indexed,
            "pending": max(0, eligible - indexed),
            "coverage": indexed / eligible if eligible else 1.0,
        }

    def _remove_stale(self, client: Any, candidate_ids: set[str]) -> int:
        if not self._collection_exists(client):
            return 0
        stale_points: list[Any] = []
        offset: Any | None = None
        while True:
            points, offset = client.scroll(
                collection_name=self.settings.qdrant_collection,
                limit=256,
                offset=offset,
                with_payload=["chunk_id"],
                with_vectors=False,
            )
            for point in points:
                chunk_id = str((point.payload or {}).get("chunk_id") or "")
                if not chunk_id or chunk_id not in candidate_ids:
                    stale_points.append(point.id)
            if offset is None:
                break
        if stale_points:
            from qdrant_client.models import PointIdsList

            client.delete(
                collection_name=self.settings.qdrant_collection,
                points_selector=PointIdsList(points=stale_points),
                wait=True,
            )
        return len(stale_points)

    def _payload(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "chunk_id": item["chunk_id"],
            "document_id": item["document_id"],
            "source_id": item["source_id"],
            "domain": item["domain"],
            "privacy": item["privacy"],
            "source_type": item["source_type"],
            "status": "indexed",
            "embedding_version": (
                f"{self.embedding.provider_name}:{self.embedding.model_name}"
            ),
        }

    def _write_state(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        now = utc_now()
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO vector_index_state(
                    chunk_id, point_id, content_hash, provider, model,
                    collection_name, payload_version, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["chunk_id"],
                        _point_id(item["chunk_id"]),
                        _content_hash(item),
                        self.embedding.provider_name,
                        self.embedding.model_name,
                        self.settings.qdrant_collection,
                        self.payload_version,
                        now,
                    )
                    for item in batch
                ],
            )
            connection.commit()

    def _refresh_payloads(
        self,
        client: Any,
        items: list[dict[str, Any]],
        batch_size: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        from qdrant_client.models import PointStruct

        refreshed = 0
        missing: list[dict[str, Any]] = []
        by_point = {_point_id(item["chunk_id"]): item for item in items}
        point_ids = list(by_point)
        for offset in range(0, len(point_ids), batch_size):
            ids = point_ids[offset : offset + batch_size]
            records = client.retrieve(
                collection_name=self.settings.qdrant_collection,
                ids=ids,
                with_payload=False,
                with_vectors=True,
            )
            found = {str(record.id): record for record in records}
            points = []
            refreshed_items = []
            for point_id in ids:
                item = by_point[point_id]
                record = found.get(point_id)
                if record is None or record.vector is None:
                    missing.append(item)
                    continue
                points.append(
                    PointStruct(
                        id=point_id,
                        vector=record.vector,
                        payload=self._payload(item),
                    )
                )
                refreshed_items.append(item)
            if points:
                client.upsert(
                    collection_name=self.settings.qdrant_collection,
                    points=points,
                    wait=True,
                )
                self._write_state(refreshed_items)
                refreshed += len(points)
        return refreshed, missing

    def sync(self, *, max_chunks: int | None = None) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "warning", "indexed": 0, "skipped": 0,
                    "warning": "云端 Embedding 尚未在本机启用或配置。"}
        candidates = self._candidates()
        with self.database.connect() as connection:
            state_rows = connection.execute(
                """
                SELECT chunk_id, point_id, content_hash, payload_version
                FROM vector_index_state
                WHERE provider = ? AND model = ? AND collection_name = ?
                """,
                (
                    self.embedding.provider_name,
                    self.embedding.model_name,
                    self.settings.qdrant_collection,
                ),
            ).fetchall()
        state = {row["chunk_id"]: dict(row) for row in state_rows}
        candidate_ids = {item["chunk_id"] for item in candidates}
        content_changed_all = [
            item for item in candidates
            if item["chunk_id"] not in state
            or state[item["chunk_id"]]["content_hash"]
            != _content_hash(item)
        ]
        content_changed_ids = {item["chunk_id"] for item in content_changed_all}
        payload_only_all = [
            item
            for item in candidates
            if item["chunk_id"] not in content_changed_ids
            and int(state[item["chunk_id"]].get("payload_version") or 1)
            != self.payload_version
        ]
        work = [("content", item) for item in content_changed_all] + [
            ("payload", item) for item in payload_only_all
        ]
        selected = work[:max_chunks] if max_chunks is not None else work
        content_changed = [item for kind, item in selected if kind == "content"]
        payload_only = [item for kind, item in selected if kind == "payload"]
        pending = len(work) - len(selected)
        client = self._get_client()
        if self._collection_exists(client):
            self._ensure_payload_indexes(client)
        removed = self._remove_stale(client, candidate_ids)
        with self.database.connect() as connection:
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                connection.execute(
                    f"""
                    DELETE FROM vector_index_state
                    WHERE provider = ? AND model = ? AND collection_name = ?
                      AND chunk_id NOT IN ({placeholders})
                    """,
                    (
                        self.embedding.provider_name,
                        self.embedding.model_name,
                        self.settings.qdrant_collection,
                        *sorted(candidate_ids),
                    ),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM vector_index_state
                    WHERE provider = ? AND model = ? AND collection_name = ?
                    """,
                    (
                        self.embedding.provider_name,
                        self.embedding.model_name,
                        self.settings.qdrant_collection,
                    ),
                )
            connection.commit()
        if not selected:
            return {
                "status": "completed",
                "indexed": 0,
                "unchanged": len(candidates),
                "removed": removed,
                "pending": pending,
            }

        indexed = 0
        payload_refreshed = 0
        batch_size = max(1, min(self.settings.embedding_batch_size, 128))
        from qdrant_client.models import PointStruct

        if payload_only:
            if not self._collection_exists(client):
                content_changed.extend(payload_only)
            else:
                payload_refreshed, missing = self._refresh_payloads(
                    client,
                    payload_only,
                    batch_size,
                )
                content_changed.extend(missing)
                indexed += payload_refreshed

        embedded = 0
        for offset in range(0, len(content_changed), batch_size):
            batch = content_changed[offset : offset + batch_size]
            try:
                vectors = self.embedding.embed(
                    [_embedding_text(item) for item in batch]
                )
            except EmbeddingError as exc:
                raise VectorIndexError(str(exc)) from None
            self._ensure_collection(client, len(vectors[0]))
            points = []
            for item, vector in zip(batch, vectors, strict=True):
                points.append(
                    PointStruct(
                        id=_point_id(item["chunk_id"]),
                        vector=vector,
                        payload=self._payload(item),
                    )
                )
            client.upsert(
                collection_name=self.settings.qdrant_collection,
                points=points,
                wait=True,
            )
            self._write_state(batch)
            indexed += len(batch)
            embedded += len(batch)
        return {
            "status": "completed",
            "indexed": indexed,
            "unchanged": len(candidates) - indexed,
            "removed": removed,
            "pending": pending,
            "embedded": embedded,
            "payload_refreshed": payload_refreshed,
            "model": self.embedding.model_name,
            "collection": self.settings.qdrant_collection,
        }

    def search(
        self,
        query: str,
        *,
        domain: str | None,
        limit: int,
        include_restricted: bool,
    ) -> VectorSearchResult:
        if not self.enabled:
            return VectorSearchResult([], "disabled")
        client = self._get_client()
        if not self._collection_exists(client):
            return VectorSearchResult([], "not_indexed", "向量集合尚未建立，已使用全文检索。")
        try:
            vector = self.embedding.embed([query])[0]
        except EmbeddingError as exc:
            return VectorSearchResult([], "warning", str(exc))

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = []
        exclusions = []
        if domain:
            conditions.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
        if not include_restricted:
            exclusions.append(
                FieldCondition(key="privacy", match=MatchValue(value="restricted"))
            )
        query_filter = (
            Filter(must=conditions, must_not=exclusions)
            if conditions or exclusions
            else None
        )
        try:
            response = client.query_points(
                collection_name=self.settings.qdrant_collection,
                query=vector,
                query_filter=query_filter,
                limit=max(1, min(limit, 100)),
                with_payload=True,
            )
        except Exception:
            return VectorSearchResult([], "warning", "Qdrant 语义检索失败，已使用全文检索。")
        chunk_ids = [str((point.payload or {}).get("chunk_id") or "") for point in response.points]
        hydrated = self.repository.hydrate_chunks(chunk_ids)
        items: list[dict[str, Any]] = []
        for point in response.points:
            payload = dict(point.payload or {})
            chunk_id = str(payload.get("chunk_id") or "")
            item = hydrated.get(chunk_id)
            if not item:
                continue
            privacy = str(item.get("privacy") or "private")
            if not include_restricted and privacy == "restricted":
                continue
            item.pop("ranking_text", None)
            item.update(
                {
                    "score": float(point.score),
                    "vector_score": float(point.score),
                    "match_strategy": "vector_semantic",
                }
            )
            items.append(item)
        return VectorSearchResult(items, "ready")
