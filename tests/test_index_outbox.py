from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pkas.core_worker import CoreWorkerLock
from pkas.index_outbox import IndexOutbox
from pkas.system import KnowledgeSystem
from pkas.vector_index import VectorIndexError


class SuccessfulVectorIndex:
    enabled = True

    def __init__(self):
        self.calls = []

    def sync(self, *, max_chunks: int | None = None) -> dict[str, Any]:
        self.calls.append(max_chunks)
        assert max_chunks is not None
        return {"status": "completed", "pending": 0}

    def coverage(self) -> dict[str, Any]:
        return {"eligible": 1, "indexed": 1, "pending": 0, "coverage": 1.0}


class FailingVectorIndex(SuccessfulVectorIndex):
    def sync(self, *, max_chunks: int | None = None) -> dict[str, Any]:
        raise VectorIndexError("temporary vector outage")


class ScopedVectorSettings:
    embedding_scope = "selected_l3"
    embedding_allow_restricted_remote_processing = False


class ScopedVectorIndex(SuccessfulVectorIndex):
    settings = ScopedVectorSettings()


def test_chunk_commit_enqueues_and_worker_completes_vector_event(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "outbox.md"
    note.write_text("事务提交后必须可靠刷新向量索引。", encoding="utf-8")

    knowledge_system.ingestion.import_file(note, domain="work", privacy="private")
    pending = knowledge_system.outbox.stats()
    vector = SuccessfulVectorIndex()
    outbox = IndexOutbox(knowledge_system.database, vector)  # type: ignore[arg-type]
    result = outbox.process()

    assert pending["pending"] >= 1
    assert result["status"] == "completed"
    assert result["completed"] >= 1
    assert result["stats"]["pending"] == 0
    assert vector.calls == [500]


def test_vector_failure_requeues_claimed_events_without_losing_them(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "retry.md"
    note.write_text("失败事件需要指数退避并保留错误码。", encoding="utf-8")
    knowledge_system.ingestion.import_file(note, domain="work", privacy="private")
    outbox = IndexOutbox(knowledge_system.database, FailingVectorIndex())  # type: ignore[arg-type]

    result = outbox.process()

    assert result["status"] == "warning"
    assert result["stats"]["pending"] >= 1
    assert result["stats"]["processing"] == 0
    with knowledge_system.database.connect() as connection:
        row = connection.execute(
            "SELECT attempts, last_error_code FROM index_outbox WHERE status='pending' LIMIT 1"
        ).fetchone()
    assert row["attempts"] == 1
    assert row["last_error_code"] == "VectorIndexError"


def test_core_worker_lock_allows_only_one_owner(test_settings: Any) -> None:
    path = test_settings.data_root / "runtime" / "test-core.lock"
    first = CoreWorkerLock(path)
    second = CoreWorkerLock(path)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()


def test_codex_user_task_does_not_enqueue_vector_work(
    knowledge_system: KnowledgeSystem,
) -> None:
    knowledge_system.database.initialize()

    knowledge_system.ingestion.import_text(
        text="请检查当前项目测试状态。",
        title="Codex user task",
        original_uri="codex://thread/turn",
        source_type="codex-turn",
        domain="work",
        privacy="private",
        metadata={"record_kind": "user_task", "assistant_output_indexed": False},
    )

    assert knowledge_system.outbox.stats()["pending"] == 0


def test_successful_full_reconciliation_closes_old_scope_events(
    knowledge_system: KnowledgeSystem,
    source_root: Path,
) -> None:
    note = source_root / "scope.md"
    note.write_text("只保留全文，不进入语义向量范围。", encoding="utf-8")
    knowledge_system.ingestion.import_file(note, domain="work", privacy="private")
    now = datetime.now(UTC).isoformat()
    with knowledge_system.database.connect() as connection:
        chunk_id = connection.execute("SELECT id FROM chunks LIMIT 1").fetchone()[0]
        connection.execute(
            "UPDATE chunks SET id=id WHERE id=?", (chunk_id,)
        )
        connection.execute(
            "UPDATE index_outbox SET updated_at=? WHERE status='pending'", (now,)
        )
        connection.commit()

    vector = ScopedVectorIndex()
    outbox = IndexOutbox(knowledge_system.database, vector)  # type: ignore[arg-type]
    result = outbox.process()

    assert result["reconciled"] is True
    assert result["completed"] >= 1
    assert result["resolved_events"]["completed"] >= 0
    assert "deferred" in result["stats"]
